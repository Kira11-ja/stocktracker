"""資料來源瀑布。每個 module 都實作 base.Source 的介面，sync.py 依 config 的順序逐層嘗試。

原則（沿用既有 Colab 的做法，但補上兩條規則）：
  1. 只補缺的：後面的來源只填前面來源沒有的期別，不覆寫已有的值。
  2. 同口徑遞補：只有同一種會計口徑之間可以互補。
     跨口徑遞補（street EPS 拿不到 → 退回 GAAP EPS）必須在 eps_basis 欄留下記號。
"""
from . import base, yf, yq, av, sec

REGISTRY = {"yf": yf, "yq": yq, "av": av, "sec": sec}


def chain(names):
    return [REGISTRY[n] for n in names if n in REGISTRY]
