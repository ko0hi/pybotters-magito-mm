from __future__ import annotations
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from pybotters.store import DataStore, Item

import asyncio
import loguru
import pybotters

from argparse import ArgumentParser
from functools import partial


#
# 状態管理
#
class Status:
    def __init__(
        self,
        store: pybotters.bitFlyerDataStore,
        max_position: int = 1,
    ):
        self._store: pybotters.bitFlyerDataStore = store
        self._asks = None
        self._bids = None
        self._max_position = max_position

        asyncio.create_task(self.auto_update_board())

    async def auto_update_board(self):
        """板情報の自動更新タスク"""
        with self._store.board.watch() as stream:
            async for msg in stream:
                self._asks, self._bids = self._store.board.sorted().values()

    def get_limit_price(self, side: str, t: float, d: int = 1):
        """注文サイズの累積量が``t``を超えたところに``d``だけ離して指値をだす。

        :param str side: ask or bid
        :param float t: 累積注文サイズの閾値
        :param int d: 参照注文からのマージン
        :return: 指値
        """
        items = self._asks if side == "ask" else self._bids
        cum_size, price = items[0]["size"], items[0]["price"]
        for i in items:
            if cum_size >= t:
                return int(price + d if side == "ask" else price - d)
            price = i['price']
            cum_size += i["size"]
        # 最後までthresholdを満たさなかった場合、一番後ろの注文と同じ額
        return int(items[-1]["price"])

    def positions(self, side: str):
        """保有ポジションリスト。

        :param str side: BUY or SELL
        :return: ポジションのlist
        """
        positions = self._store.positions.find({"side": side})
        assert len(positions) <= self._max_position
        return positions

    def remaining_size(self, side):
        """保有ポジションサイズ。

        :param str side: BUY or SELL
        :return: ポジションサイズ
        """
        positions = self.positions(side)
        if len(positions):
            return sum([p["size"] for p in positions])
        else:
            return 0

    @property
    def best_ask(self):
        return int(self._asks[0]["price"])

    @property
    def best_bid(self):
        return int(self._bids[0]["price"])

    @property
    def spread(self):
        return (self.best_ask - self.best_bid) / self.best_bid


#
# Event監視
#
class EventWatcher:
    def __init__(self, store: DataStore, trigger_fn: Callable[[Item], bool] = None):
        self._store = store
        self._trigger_fn = trigger_fn
        self._task = asyncio.create_task(self._watch())

    async def _watch(self):
        """`_is_target_event(msg.data)`がTrueを返すまでDataStoreをwatchし続ける。"""
        with self._store.watch() as stream:
            async for msg in stream:
                if self._is_trigger(msg.data):
                    return msg.data

    def _is_trigger(self, d: Item):
        """socketメッセージを受け取って、イベント発火の有無を判定する。
        子クラスこの関数をオーバーライドしてもいいし、`trigger_fn`として与えてもいい。

        :param Item d: socketメッセージ。
        :return:
        """
        if self._trigger_fn is None:
            raise NotImplementedError
        return self._trigger_fn(d)

    async def wait(self):
        await self._task

    def done(self):
        return self._task.done()

    def result(self):
        return self._task.result()


class ChildOrderEventWatcher(EventWatcher):
    def __init__(self, store, order_id, **kwargs):
        self._order_id = order_id
        self._cond = kwargs
        super(ChildOrderEventWatcher, self).__init__(store)

    def _is_trigger(self, d):
        return d["child_order_acceptance_id"] == self._order_id and all(
            [v == d[k] for (k, v) in self._cond.items()]
        )

    def replace_order_id(self, order_id):
        self._order_id = order_id


class ExecutionWatcher(ChildOrderEventWatcher):
    def __init__(self, store, order_id):
        super(ExecutionWatcher, self).__init__(store, order_id, event_type="EXECUTION")


class CancelWatcher(ChildOrderEventWatcher):
    def __init__(self, store, order_id):
        super(CancelWatcher, self).__init__(store, order_id)

    def _is_trigger(self, d):
        return d["child_order_acceptance_id"] == self._order_id and d["event_type"] in [
            "CANCEL",
            "CANCEL_FAILED",
        ]


#
# 注文ヘルパー
#
async def limit_order(client, symbol, side, size, price, time_in_force="GTC"):
    assert side in ["BUY", "SELL"]
    res = await client.post(
        "/v1/me/sendchildorder",
        data={
            "product_code": symbol,
            "side": side,
            "size": size,
            "child_order_type": "LIMIT",
            "price": int(price),
            "time_in_force": time_in_force,
        },
    )

    data = await res.json()

    if res.status != 200:
        raise RuntimeError(f"Invalid request: {data}")
    else:
        return data["child_order_acceptance_id"]


async def cancel_order(client, symbol, order_id):
    order_id_key = "child_order_id"
    if order_id.startswith("JRF"):
        order_id_key = order_id_key.replace("_id", "_acceptance_id")

    res = await client.post(
        "/v1/me/cancelchildorder", data={"product_code": symbol, order_id_key: order_id}
    )

    return res.status == 200


#
# mmロジック
#
async def market_making(
    client,
    store: pybotters.bitFlyerDataStore,
    status: Status,
    symbol: str,
    t: float,
    d: int,
    s_entry: float,
    s_update: float,
    size: float,
    logger: loguru.Logger,
):
    """

    :param client: クライアント
    :param store: データストア
    :param status: 状態
    :param symbol: product_code
    :param t: 累積サイズの閾値
    :param d: 参照注文からのマージン
    :param s_entry: エントリー条件のスプレッド
    :param s_update: 指値更新条件のスプレッド
    :param size: 注文サイズ
    :param logger: ロガー
    :return:
    """

    async def _oneside_loop(side: str, size: float, pricer: Callable[[], int]):
        """片サイドの注文→キャンセル→再注文ループ。

        :param side: "BUY" or "SELL"
        :param size: 注文サイズ
        :param pricer: 指値関数
        :return:
        """
        # エントリー
        price = pricer()
        order_id = await limit_order(client, symbol, side, size, price)
        logger.info(f"[{side} ENTRY] {order_id} / {price} / {size:.5f}")

        # 約定監視ループ
        execution_watcher = ExecutionWatcher(store.childorderevents, order_id)
        while not execution_watcher.done():

            # 指値更新間隔
            # １ループでキャンセルと指値更新を最大２回x２（両サイド）行う。API制限が500/5minなので、
            # 300 / 3.5 * 4 = 342.85... とちょっと余裕あるくらいに設定しておく。
            # （余談）途中でcontinueとかよくするのでsleepはwhileの直下で実行するのが個人的に好き
            await asyncio.sleep(3.5)

            # spreadが閾値以上なので指値更新
            if status.spread > s_update:
                new_price = pricer()
                if price != new_price:
                    # 前の注文をキャンセル。childorvereventsをwatchしてCANCEL or
                    # CANCEL_FAILEDのステータスを確認してから次の注文を入れる
                    cancel_watcher = CancelWatcher(store.childorderevents, order_id)
                    is_canceled = await cancel_order(client, symbol, order_id)
                    await cancel_watcher.wait()

                    cancel_result = cancel_watcher.result()

                    if cancel_result["event_type"] == "CANCEL":
                        # キャンセル成功→再注文
                        logger.info(f"[{side} CANCELED] {order_id}")
                        new_order_id = await limit_order(
                            client, symbol, side, size, new_price
                        )

                        # 監視する注文番号・指値を更新
                        execution_watcher.replace_order_id(new_order_id)
                        order_id = new_order_id
                        price = new_price
                        logger.info(f"[{side} UPDATE] {order_id} / {price}")

                    elif cancel_result["event_type"] == "CANCEL_FAILED":
                        # キャンセル失敗→約定しているはずなので次のループでexecution_watcherがdoneになる
                        logger.info(
                            f"[{side} CANCEL FAILED] {order_id} (should be executed)"
                        )
                        continue

        # 約定
        loop_result = execution_watcher.result()
        logger.info(f"[{side} FINISH] {loop_result}")
        return loop_result

    while True:
        sp = status.spread
        if sp > s_entry:
            # 現在のポジションから端数を取得
            buy_remaining_size = status.remaining_size("BUY")
            sell_remaining_size = status.remaining_size("SELL")

            logger.info(
                f"[START]\n"
                f"\tspread: {sp}\n"
                f"\tsymbol: {symbol}\n"
                f"\tt: {t}\n"
                f"\td: {d}\n"
                f"\ts_entry: {s_entry}\n"
                f"\ts_update: {s_update}\n"
                f"\tbuy_size: {size + sell_remaining_size}\n"
                f"\tsell_size: {size + buy_remaining_size}"
            )

            buy_result, sell_result = await asyncio.gather(
                _oneside_loop(
                    "BUY",
                    size + sell_remaining_size,
                    partial(status.get_limit_price, "bid", t, d),
                ),
                _oneside_loop(
                    "SELL",
                    size + buy_remaining_size,
                    partial(status.get_limit_price, "ask", t, d),
                ),
            )

            logger.info(f"[FINISH] {sell_result['price'] - buy_result['price']}")
            break
        else:
            logger.info(
                f"[WAITING CHANCE] {status.best_ask} - ({status.spread:.4f}) - {status.best_bid}"
            )

            await asyncio.sleep(0.1)


#
# main
#
async def main(args):
    logger = loguru.logger
    logger.add("log.txt", rotation="10MB", retention=3)

    async with pybotters.Client(
        apis=args.api_key_json, base_url="https://api.bitflyer.com"
    ) as client:

        store = pybotters.bitFlyerDataStore()
        status = Status(store)

        wstask = await client.ws_connect(
            "wss://ws.lightstream.bitflyer.com/json-rpc",
            send_json=[
                {
                    "method": "subscribe",
                    "params": {"channel": "lightning_board_snapshot_FX_BTC_JPY"},
                    "id": 1,
                },
                {
                    "method": "subscribe",
                    "params": {"channel": "lightning_board_FX_BTC_JPY"},
                    "id": 2,
                },
                {
                    "method": "subscribe",
                    "params": {"channel": "child_order_events"},
                    "id": 3,
                },
            ],
            hdlr_json=store.onmessage,
        )

        while not all([len(w) for w in [store.board]]):
            logger.debug("[WAITING SOCKET RESPONSE]")
            await store.wait()

        while True:
            await market_making(
                client,
                store,
                status,
                args.symbol,
                args.t,
                args.d,
                args.s_entry,
                args.s_update,
                args.lot,
                logger,
            )

            await asyncio.sleep(args.interval)


if __name__ == "__main__":

    parser = ArgumentParser()
    parser.add_argument("--api_key_json")
    parser.add_argument("--symbol", default="FX_BTC_JPY")
    parser.add_argument("--lot", default=0.01, type=float)
    parser.add_argument("--t", default=0.03, type=float)
    parser.add_argument("--d", default=1, type=int)
    parser.add_argument("--s_entry", default=0.0004, type=float)
    parser.add_argument("--s_update", default=0.0003, type=float)
    parser.add_argument("--interval", default=5, type=int)

    args = parser.parse_args()

    try:
        asyncio.run(main(args))
    except KeyboardInterrupt as e:
        pass
