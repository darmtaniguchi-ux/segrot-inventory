"""
セグロット在庫連携アプリ — データ層（最小形）。

設計:
  - products: 商品マスタ（Excelの商品コード=主キー / 商品名 / 入数 / 期首残）
  - movements: 日次入出庫（日付 / 商品 / 種別in|out / 個数）。在庫の正本はこの履歴。
  - 在庫数 = 期首残 + Σ入荷 − Σ出荷（個数で統一して計算）。
  - 入力単位はカートン/個数の両方可。カートンは入数を掛けて個数に換算して保存する
    （換算はAPI層で行い、movementsには必ず個数で記録）。

このフェーズではクラウドPaaSへの配置を前提に、SQLiteファイルで動く。
将来データ量が増えたらPostgreSQL等へ移行可能（構造は流用できる）。
"""
import sqlite3
import os
import json
from datetime import datetime, timezone, date

# DB_PATH環境変数で永続ディスクの場所を指定できる(例: Renderで /data をマウントし
# DB_PATH=/data/segrot_inventory.db とする)。未設定時はこれまで通りアプリ直下。
DB_PATH = os.environ.get(
    "DB_PATH", os.path.join(os.path.dirname(__file__), "..", "segrot_inventory.db")
)
SEED_PATH = os.path.join(os.path.dirname(__file__), "seed_products.json")

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    code             TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    jan              TEXT,
    units_per_carton INTEGER NOT NULL DEFAULT 1,
    opening          INTEGER NOT NULL DEFAULT 0,   -- 期首残(導入時点の在庫)
    discontinued     INTEGER NOT NULL DEFAULT 0,
    sort_order       INTEGER NOT NULL DEFAULT 0,
    moq              INTEGER NOT NULL DEFAULT 0    -- メーカー発注単位(felicross_fbaのメーカー発注判定で使用。0=未設定)
);
CREATE INDEX IF NOT EXISTS idx_prod_jan ON products(jan);

CREATE TABLE IF NOT EXISTS movements (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT NOT NULL,
    move_date   TEXT NOT NULL,                     -- YYYY-MM-DD
    kind        TEXT NOT NULL CHECK(kind IN ('in','out')),
    qty         INTEGER NOT NULL,                  -- 必ず個数で保存
    input_unit  TEXT NOT NULL DEFAULT 'pcs',       -- 入力時の単位(履歴用): 'pcs' or 'carton'
    input_value INTEGER NOT NULL DEFAULT 0,        -- 入力時の値(履歴用)
    note        TEXT,
    created_at  TEXT NOT NULL,
    created_by  TEXT,
    FOREIGN KEY (code) REFERENCES products(code)
);
CREATE INDEX IF NOT EXISTS idx_mv_code ON movements(code);
CREATE INDEX IF NOT EXISTS idx_mv_date ON movements(move_date);

-- FBA発送報告(セグロット専用入力画面のデータ)。
-- 現時点ではAmazonへのAPI登録は未実装。ここで構造化して保存し、
-- 認証情報が整ったら別処理がこのデータを読んでFulfillment Inbound APIへ登録する想定。
CREATE TABLE IF NOT EXISTS fba_shipments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'reported',  -- reported(報告済・API未連携) / registered(将来: Amazon登録済)
    carrier      TEXT,                              -- 配送業者名(自社便の場合)
    note         TEXT,
    created_by   TEXT
);

CREATE TABLE IF NOT EXISTS fba_shipment_items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    shipment_id  INTEGER NOT NULL,
    code         TEXT NOT NULL,       -- 商品コード
    cartons      INTEGER NOT NULL,
    qty          INTEGER NOT NULL,    -- cartons × 入数
    FOREIGN KEY (shipment_id) REFERENCES fba_shipments(id)
);

CREATE TABLE IF NOT EXISTS fba_shipment_boxes (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    shipment_id    INTEGER NOT NULL,
    box_no         INTEGER NOT NULL,      -- 箱の通し番号
    length_cm      REAL,
    width_cm       REAL,
    height_cm      REAL,
    weight_kg      REAL,
    tracking_number TEXT,                 -- 追跡番号(発送後に入力・後から更新可)
    FOREIGN KEY (shipment_id) REFERENCES fba_shipments(id)
);
"""


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_conn()
    try:
        conn.executescript(SCHEMA)

        # 既存DBへのマイグレーション: moq列が無ければ追加(旧スキーマ互換)。
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(products)").fetchall()}
        if "moq" not in cols:
            conn.execute("ALTER TABLE products ADD COLUMN moq INTEGER NOT NULL DEFAULT 0")

        n = conn.execute("SELECT COUNT(*) c FROM products").fetchone()["c"]
        if n == 0 and os.path.exists(SEED_PATH):
            with open(SEED_PATH, encoding="utf-8") as f:
                items = json.load(f)
            for i, it in enumerate(items):
                conn.execute(
                    "INSERT OR REPLACE INTO products (code,name,jan,units_per_carton,opening,sort_order,moq) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (it["code"], it["name"], it.get("jan") or None,
                     int(it.get("units_per_carton") or 1),
                     int(it.get("opening") or 0), i,
                     int(it.get("moq") or 0)),
                )
            conn.commit()
    finally:
        conn.close()


def list_products():
    conn = get_conn()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM products WHERE discontinued=0 ORDER BY sort_order"
        ).fetchall()]
    finally:
        conn.close()


def current_stock():
    """各商品の現在在庫（個数）と当日の入出庫を返す。"""
    today = date.today().isoformat()
    conn = get_conn()
    try:
        rows = conn.execute("""
            SELECT p.code, p.name, p.units_per_carton, p.opening,
                   COALESCE(SUM(CASE WHEN m.kind='in'  THEN m.qty END),0) AS total_in,
                   COALESCE(SUM(CASE WHEN m.kind='out' THEN m.qty END),0) AS total_out,
                   COALESCE(SUM(CASE WHEN m.kind='in'  AND m.move_date=? THEN m.qty END),0) AS today_in,
                   COALESCE(SUM(CASE WHEN m.kind='out' AND m.move_date=? THEN m.qty END),0) AS today_out
            FROM products p
            LEFT JOIN movements m ON m.code = p.code
            WHERE p.discontinued=0
            GROUP BY p.code
            ORDER BY p.sort_order
        """, (today, today)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["stock"] = d["opening"] + d["total_in"] - d["total_out"]
            out.append(d)
        return out
    finally:
        conn.close()


def add_movement(code, kind, input_unit, input_value, note=None, created_by=None):
    """入出庫を登録。カートン入力は個数へ換算して保存。"""
    if kind not in ("in", "out"):
        raise ValueError("kind must be in/out")
    if input_unit not in ("pcs", "carton"):
        raise ValueError("unit must be pcs/carton")
    if input_value <= 0:
        raise ValueError("value must be > 0")

    conn = get_conn()
    try:
        prod = conn.execute("SELECT * FROM products WHERE code=?", (code,)).fetchone()
        if not prod:
            raise ValueError("unknown product code")
        upc = prod["units_per_carton"] or 1
        qty = input_value * upc if input_unit == "carton" else input_value

        # 出庫が在庫を超える場合は警告対象（登録は許すが呼び出し側で確認）
        conn.execute(
            "INSERT INTO movements (code,move_date,kind,qty,input_unit,input_value,note,created_at,created_by) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (code, date.today().isoformat(), kind, qty, input_unit, input_value,
             note, datetime.now(timezone.utc).isoformat(), created_by),
        )
        conn.commit()
        return {"code": code, "kind": kind, "qty": qty, "unit": input_unit, "value": input_value}
    finally:
        conn.close()


def set_stock(code, target_stock, created_by=None, note=None):
    """
    棚卸等の実地カウント結果に在庫を合わせる。
    現在庫との差分を1件の調整movement(in/out)として記録する(手入力の履歴と同じ扱い)。
    差分が0の場合は何も記録しない。
    """
    conn = get_conn()
    try:
        prod = conn.execute("SELECT * FROM products WHERE code=?", (code,)).fetchone()
        if not prod:
            raise ValueError("unknown product code")
        row = conn.execute("""
            SELECT p.opening,
                   COALESCE(SUM(CASE WHEN m.kind='in'  THEN m.qty END),0) AS total_in,
                   COALESCE(SUM(CASE WHEN m.kind='out' THEN m.qty END),0) AS total_out
            FROM products p LEFT JOIN movements m ON m.code = p.code
            WHERE p.code = ?
        """, (code,)).fetchone()
        current = row["opening"] + row["total_in"] - row["total_out"]
        delta = int(target_stock) - current
        if delta == 0:
            return {"code": code, "before": current, "after": target_stock, "adjusted": 0}

        kind = "in" if delta > 0 else "out"
        qty = abs(delta)
        conn.execute(
            "INSERT INTO movements (code,move_date,kind,qty,input_unit,input_value,note,created_at,created_by) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (code, date.today().isoformat(), kind, qty, "pcs", qty,
             note, datetime.now(timezone.utc).isoformat(), created_by),
        )
        conn.commit()
        return {"code": code, "before": current, "after": target_stock, "adjusted": delta}
    finally:
        conn.close()


def add_product(code, name, units_per_carton, jan=None, opening=0, moq=0):
    """新商品をマスタに追加する（管理者用）。
    moq: メーカー発注単位。felicross_fba側のメーカー発注判定で使用する（未入力可・0=未設定）。
    """
    code = str(code).strip()
    name = str(name).strip()
    if not code or not name:
        raise ValueError("商品コードと商品名は必須です")
    conn = get_conn()
    try:
        exists = conn.execute("SELECT 1 FROM products WHERE code=?", (code,)).fetchone()
        if exists:
            raise ValueError(f"商品コード {code} は既に登録されています")
        if jan:
            jan = str(jan).strip()
            dup = conn.execute("SELECT code FROM products WHERE jan=?", (jan,)).fetchone()
            if dup:
                raise ValueError(f"JAN {jan} は既に「{dup['code']}」に登録されています")
        maxo = conn.execute("SELECT COALESCE(MAX(sort_order),0) m FROM products").fetchone()["m"]
        conn.execute(
            "INSERT INTO products (code,name,jan,units_per_carton,opening,sort_order,moq) VALUES (?,?,?,?,?,?,?)",
            (code, name, jan or None, int(units_per_carton or 1), int(opening or 0), maxo + 1, int(moq or 0)),
        )
        conn.commit()
        return {"code": code, "name": name, "jan": jan, "units_per_carton": int(units_per_carton or 1),
                "opening": int(opening or 0), "moq": int(moq or 0)}
    finally:
        conn.close()


def find_by_jan(jan):
    """JANコードから商品を1件特定する（バーコード読み取り用）。"""
    conn = get_conn()
    try:
        r = conn.execute(
            "SELECT * FROM products WHERE jan=? AND discontinued=0", (str(jan).strip(),)
        ).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def recent_movements(limit=30, code=None, kind=None, date_from=None, date_to=None):
    """
    入出庫履歴を新しい順で返す。
    code/kind/date_from(YYYY-MM-DD)/date_to(YYYY-MM-DD)で絞り込み可能(すべて任意)。
    """
    conditions, params = [], []
    if code:
        conditions.append("m.code = ?")
        params.append(code)
    if kind:
        conditions.append("m.kind = ?")
        params.append(kind)
    if date_from:
        conditions.append("m.move_date >= ?")
        params.append(date_from)
    if date_to:
        conditions.append("m.move_date <= ?")
        params.append(date_to)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    conn = get_conn()
    try:
        rows = conn.execute(f"""
            SELECT m.*, p.name FROM movements m
            JOIN products p ON p.code=m.code
            {where}
            ORDER BY m.id DESC LIMIT ?
        """, (*params, limit)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def create_fba_shipment(items, boxes, carrier=None, note=None, created_by=None):
    """
    FBA発送報告を登録する（セグロット専用入力画面から）。
    items: [{"code":.., "cartons":..}] — 発送した商品とカートン数
    boxes: [{"length_cm":.., "width_cm":.., "height_cm":.., "weight_kg":.., "tracking_number":..}]
    現時点ではAmazonへのAPI登録は行わない。構造化して保存するのみ。
    """
    if not items:
        raise ValueError("発送商品が指定されていません")
    if not boxes:
        raise ValueError("箱の情報が指定されていません")

    conn = get_conn()
    try:
        code_to_upc = {}
        for it in items:
            code = str(it["code"]).strip()
            row = conn.execute("SELECT units_per_carton FROM products WHERE code=?", (code,)).fetchone()
            if not row:
                raise ValueError(f"商品コード {code} が見つかりません")
            code_to_upc[code] = row["units_per_carton"]

        now = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            "INSERT INTO fba_shipments (created_at, status, carrier, note, created_by) VALUES (?,?,?,?,?)",
            (now, "reported", carrier, note, created_by),
        )
        shipment_id = cur.lastrowid

        for it in items:
            code = str(it["code"]).strip()
            cartons = int(it["cartons"])
            if cartons <= 0:
                raise ValueError(f"{code} のカートン数は1以上にしてください")
            qty = cartons * code_to_upc[code]
            conn.execute(
                "INSERT INTO fba_shipment_items (shipment_id, code, cartons, qty) VALUES (?,?,?,?)",
                (shipment_id, code, cartons, qty),
            )

        for i, box in enumerate(boxes, 1):
            conn.execute(
                "INSERT INTO fba_shipment_boxes "
                "(shipment_id, box_no, length_cm, width_cm, height_cm, weight_kg, tracking_number) "
                "VALUES (?,?,?,?,?,?,?)",
                (shipment_id, i, box.get("length_cm"), box.get("width_cm"),
                 box.get("height_cm"), box.get("weight_kg"), box.get("tracking_number") or None),
            )

        conn.commit()
        return {"shipment_id": shipment_id, "item_count": len(items), "box_count": len(boxes)}
    finally:
        conn.close()


def list_fba_shipments(limit=20):
    """FBA発送報告の一覧（新しい順）。商品・箱の明細を含む。"""
    conn = get_conn()
    try:
        shipments = conn.execute(
            "SELECT * FROM fba_shipments ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        result = []
        for s in shipments:
            sid = s["id"]
            items = conn.execute(
                "SELECT si.code, si.cartons, si.qty, p.name FROM fba_shipment_items si "
                "JOIN products p ON p.code=si.code WHERE si.shipment_id=?", (sid,)
            ).fetchall()
            boxes = conn.execute(
                "SELECT * FROM fba_shipment_boxes WHERE shipment_id=? ORDER BY box_no", (sid,)
            ).fetchall()
            d = dict(s)
            d["items"] = [dict(i) for i in items]
            d["boxes"] = [dict(b) for b in boxes]
            result.append(d)
        return result
    finally:
        conn.close()


def update_tracking_number(shipment_id, box_no, tracking_number):
    """発送後に追跡番号を追記・更新する。"""
    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE fba_shipment_boxes SET tracking_number=? WHERE shipment_id=? AND box_no=?",
            (tracking_number, shipment_id, box_no),
        )
        conn.commit()
        if cur.rowcount == 0:
            raise ValueError("該当する箱が見つかりません")
        return {"shipment_id": shipment_id, "box_no": box_no, "tracking_number": tracking_number}
    finally:
        conn.close()
