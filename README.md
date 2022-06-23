# pybotters-magito-mm

pybotters + asyncioを使った [magito MM BOT (bitflyer)](https://note.com/magimagi1223/n/n5fba7501dcfd)の実装です。


## 使い方

依存ライブラリのインストール
```bash
pip install pybotters loguru
```

API keyの入ったJSONファイル

```
# api.json
{
  "bitflyer": [
      "...",  # API Key
      "..."   # API Secrete
  ]
}
```


実行

```bash
python main.py --api_key_json PATH/TO/api.json
```

オプション

```bash
usage: main.py [-h] --api_key_json API_KEY_JSON [--symbol SYMBOL] [--lot LOT] [--t T] [--d D] [--s_entry S_ENTRY] [--s_update S_UPDATE] [--interval INTERVAL]

pybotters x asyncio x magito MM

options:
  -h, --help            show this help message and exit
  --api_key_json API_KEY_JSON
                        apiキーが入ったJSONファイル
  --symbol SYMBOL       取引通過
  --lot LOT             注文サイズ
  --t T                 板上での累積注文量に対する閾値（この閾値を超えた時点での注文価格が参照価格となる）
  --d D                 参照価格と指値のマージン（指値＝参照価格±d）
  --s_entry S_ENTRY     エントリー用のスプレッド閾値（スプレッドがこの閾値以上の時にマーケットメイキングを開始する）
  --s_update S_UPDATE   指値更新用のスプレッド閾値（スプレッドがこの閾値以上の時に指値を更新する）
  --interval INTERVAL   マーケットメイキングサイクルの間隔
```


#### 注意
自己責任での使用をお願いします。

ライセンス：MIT
