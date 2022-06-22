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


#### 注意
自己責任での使用をお願いします。
