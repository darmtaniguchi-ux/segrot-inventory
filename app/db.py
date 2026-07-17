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
    source      TEXT NOT NULL DEFAULT 'manual',    -- 'manual'=通常の入出庫 / 'adjust'=棚卸等の在庫調整(履歴には既定で表示しない)
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

-- 旧形式(発送全体でまとめた商品リスト)。新規登録では使わないが、過去データ閲覧用に残す。
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

-- 箱ごとの内容(商品コード・個数)。1つの箱に複数SKUを混載できるようにするため、
-- カートン単位ではなく個数(バラ)で持つ。
CREATE TABLE IF NOT EXISTS fba_shipment_box_items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    shipment_id  INTEGER NOT NULL,
    box_no       INTEGER NOT NULL,
    code         TEXT NOT NULL,
    qty          INTEGER NOT NULL,
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

        mv_cols = {r["name"] for r in conn.execute("PRAGMA table_info(movements)").fetchall()}
        if "source" not in mv_cols:
            conn.execute("ALTER TABLE movements ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'")

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
            "INSERT INTO movements (code,move_date,kind,qty,input_unit,input_value,note,source,created_at,created_by) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (code, date.today().isoformat(), kind, qty, input_unit, input_value,
             note, "manual", datetime.now(timezone.utc).isoformat(), created_by),
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
            "INSERT INTO movements (code,move_date,kind,qty,input_unit,input_value,note,source,created_at,created_by) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (code, date.today().isoformat(), kind, qty, "pcs", qty,
             note, "adjust", datetime.now(timezone.utc).isoformat(), created_by),
        )
        conn.commit()
        return {"code": code, "before": current, "after": target_stock, "adjusted": delta}
    finally:
        conn.close()


def add_product(code, name, units_per_carton, jan=None, opening=0, moq=0):
    """新商品をマスタに追加する（管理者用）。
    moq: メーカー発注単位。felicross_fba側のメーカー発注判定で使用する（未入力可・0=未設定）。
    jan: 同じJANを複数の商品コードで共有できる(ロット違いなどを別商品として管理するため)。
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
    """JANコードから商品を検索する（バーコード読み取り用）。
    同じJANをロット違い等で複数商品コードに登録している場合は全件返す
    （呼び出し側で1件なら自動選択、複数ならユーザーに選ばせる）。"""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM products WHERE jan=? AND discontinued=0 ORDER BY sort_order",
            (str(jan).strip(),),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def update_movement(movement_id, kind=None, input_unit=None, input_value=None, note=None, source=None):
    """入出庫履歴の修正(数量・種別・備考)。商品自体を変更したい場合は削除して登録し直す。
    source: 通常は変更不要。棚卸調整分を履歴一覧から隠す/戻すためのメンテナンス用途で使う('manual'/'adjust')。"""
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM movements WHERE id=?", (movement_id,)).fetchone()
        if not row:
            raise ValueError("該当する履歴が見つかりません")
        new_kind = kind if kind is not None else row["kind"]
        new_unit = input_unit if input_unit is not None else row["input_unit"]
        new_value = int(input_value) if input_value is not None else row["input_value"]
        new_note = note if note is not None else row["note"]
        new_source = source if source is not None else row["source"]
        if new_kind not in ("in", "out"):
            raise ValueError("kind must be in/out")
        if new_unit not in ("pcs", "carton"):
            raise ValueError("unit must be pcs/carton")
        if new_value <= 0:
            raise ValueError("value must be > 0")
        prod = conn.execute("SELECT * FROM products WHERE code=?", (row["code"],)).fetchone()
        upc = prod["units_per_carton"] or 1
        new_qty = new_value * upc if new_unit == "carton" else new_value
        conn.execute(
            "UPDATE movements SET kind=?, qty=?, input_unit=?, input_value=?, note=?, source=? WHERE id=?",
            (new_kind, new_qty, new_unit, new_value, new_note, new_source, movement_id),
        )
        conn.commit()
        return {"id": movement_id, "code": row["code"], "kind": new_kind, "qty": new_qty,
                "unit": new_unit, "value": new_value, "source": new_source}
    finally:
        conn.close()


def delete_movement(movement_id):
    """入出庫履歴を1件削除する(誤登録の取り消し)。"""
    conn = get_conn()
    try:
        cur = conn.execute("DELETE FROM movements WHERE id=?", (movement_id,))
        conn.commit()
        if cur.rowcount == 0:
            raise ValueError("該当する履歴が見つかりません")
        return {"id": movement_id, "deleted": True}
    finally:
        conn.close()


def replace_excel_movements(movements, created_by=None):
    """
    Excel一括取り込み(excel_import.parse_daily_movements)で得たmovementsを反映する。
    同じ(商品コード, 日付)の既存のexcel_import由来movementは一旦削除してから入れ直すため、
    同じシートを再アップロードして修正しても安全(重複登録にならない)。
    商品マスタに存在しないコードはスキップし、一覧を返す。
    """
    conn = get_conn()
    try:
        codes = {m["code"] for m in movements}
        if codes:
            placeholders = ",".join("?" * len(codes))
            valid_codes = {r["code"] for r in conn.execute(
                f"SELECT code FROM products WHERE code IN ({placeholders})", tuple(codes)
            ).fetchall()}
        else:
            valid_codes = set()
        skipped_codes = sorted(codes - valid_codes)

        touched_days = {(m["code"], m["date"]) for m in movements if m["code"] in valid_codes}
        for code, mdate in touched_days:
            conn.execute(
                "DELETE FROM movements WHERE code=? AND move_date=? AND source='excel_import'",
                (code, mdate),
            )

        now = datetime.now(timezone.utc).isoformat()
        applied = 0
        for m in movements:
            if m["code"] not in valid_codes:
                continue
            conn.execute(
                "INSERT INTO movements (code,move_date,kind,qty,input_unit,input_value,note,source,created_at,created_by) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (m["code"], m["date"], m["kind"], m["qty"], "pcs", m["qty"],
                 "Excel一括登録", "excel_import", now, created_by),
            )
            applied += 1
        conn.commit()
        return {"applied": applied, "skipped_codes": skipped_codes, "days_touched": len(touched_days)}
    finally:
        conn.close()


def recent_movements(limit=30, code=None, kind=None, date_from=None, date_to=None, include_adjust=False):
    """
    入出庫履歴を新しい順で返す。
    code/kind/date_from(YYYY-MM-DD)/date_to(YYYY-MM-DD)で絞り込み可能(すべて任意)。
    include_adjust=False(既定)の場合、棚卸等の在庫調整分(source='adjust')は日々の入出庫履歴には表示しない。
    """
    conditions, params = [], []
    if not include_adjust:
        conditions.append("m.source != 'adjust'")
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


def create_fba_shipment(boxes, carrier=None, note=None, created_by=None):
    """
    FBA発送報告を登録する（セグロット専用入力画面から）。
    boxes: [{"length_cm":.., "width_cm":.., "height_cm":.., "weight_kg":..,
             "items":[{"code":.., "qty":..}, ...]}]
    1つの箱に複数のSKUを混載できるよう、商品・個数は箱ごとに持つ(カートン単位に限定しない)。
    現時点ではAmazonへのAPI登録は行わない。構造化して保存するのみ。
    """
    if not boxes:
        raise ValueError("箱の情報が指定されていません")
    if not any(b.get("items") for b in boxes):
        raise ValueError("箱に商品と個数を1件以上入力してください")

    conn = get_conn()
    try:
        all_codes = {str(it["code"]).strip() for b in boxes for it in b.get("items", [])}
        for code in all_codes:
            row = conn.execute("SELECT 1 FROM products WHERE code=?", (code,)).fetchone()
            if not row:
                raise ValueError(f"商品コード {code} が見つかりません")

        now = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            "INSERT INTO fba_shipments (created_at, status, carrier, note, created_by) VALUES (?,?,?,?,?)",
            (now, "reported", carrier, note, created_by),
        )
        shipment_id = cur.lastrowid

        item_count = 0
        for i, box in enumerate(boxes, 1):
            conn.execute(
                "INSERT INTO fba_shipment_boxes "
                "(shipment_id, box_no, length_cm, width_cm, height_cm, weight_kg, tracking_number) "
                "VALUES (?,?,?,?,?,?,?)",
                (shipment_id, i, box.get("length_cm"), box.get("width_cm"),
                 box.get("height_cm"), box.get("weight_kg"), box.get("tracking_number") or None),
            )
            for it in box.get("items", []):
                code = str(it["code"]).strip()
                qty = int(it["qty"])
                if qty <= 0:
                    raise ValueError(f"{code} の個数は1以上にしてください")
                conn.execute(
                    "INSERT INTO fba_shipment_box_items (shipment_id, box_no, code, qty) VALUES (?,?,?,?)",
                    (shipment_id, i, code, qty),
                )
                item_count += 1

        conn.commit()
        return {"shipment_id": shipment_id, "item_count": item_count, "box_count": len(boxes)}
    finally:
        conn.close()


def list_fba_shipments(limit=20):
    """FBA発送報告の一覧（新しい順）。箱ごとの商品明細を含む。
    旧形式(発送全体でまとめた商品リスト)のデータも"items"として引き続き読めるようにする。"""
    conn = get_conn()
    try:
        shipments = conn.execute(
            "SELECT * FROM fba_shipments ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        result = []
        for s in shipments:
            sid = s["id"]
            legacy_items = conn.execute(
                "SELECT si.code, si.cartons, si.qty, p.name FROM fba_shipment_items si "
                "JOIN products p ON p.code=si.code WHERE si.shipment_id=?", (sid,)
            ).fetchall()
            boxes = conn.execute(
                "SELECT * FROM fba_shipment_boxes WHERE shipment_id=? ORDER BY box_no", (sid,)
            ).fetchall()
            box_list = []
            for b in boxes:
                box_items = conn.execute(
                    "SELECT bi.code, bi.qty, p.name FROM fba_shipment_box_items bi "
                    "JOIN products p ON p.code=bi.code WHERE bi.shipment_id=? AND bi.box_no=?",
                    (sid, b["box_no"]),
                ).fetchall()
                bd = dict(b)
                bd["items"] = [dict(i) for i in box_items]
                box_list.append(bd)
            d = dict(s)
            d["items"] = [dict(i) for i in legacy_items]
            d["boxes"] = box_list
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
