"""
セグロット在庫連携アプリ — バックエンド（最小形）。

簡易ログイン:
  - 環境変数で users を定義（パスワードはこのコードに書かない）。
  - 形式: APP_USERS="segrot:role=writer;felicross:role=viewer" のような最小構成は避け、
    実際のID/パスワードは環境変数 APP_LOGIN_USERS（JSON）で与える。
  - 例(本番はPaaSの環境変数で設定。ここに実値は書かない):
      APP_LOGIN_USERS = '[{"user":"segrot","role":"writer"},{"user":"felicross","role":"viewer"}]'
      APP_LOGIN_SECRETS = '{"segrot":"<ハッシュ>","felicross":"<ハッシュ>"}'
  - パスワードは平文では持たず、sha256ハッシュで比較する。
    ハッシュ生成はデプロイ手順で案内（このチャットや環境変数に平文を置かない）。

ロール:
  - writer: 入出庫を登録できる（セグロット）
  - viewer: 在庫確認のみ（フェリクロス側の閲覧）
  - admin : 両方（谷口さん）
"""
import os
import json
import hashlib
import secrets
from fastapi import FastAPI, HTTPException, Request, Response, Depends, Form
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db

app = FastAPI(title="セグロット在庫連携アプリ", version="0.1.0")
STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "static")

# ---- 簡易セッション（メモリ保持。最小形のため）----
_sessions = {}  # token -> {"user":..., "role":...}


def _load_users():
    raw = os.environ.get("APP_LOGIN_USERS", "")
    if not raw:
        # 開発用フォールバック（PaaSでは必ず環境変数で上書きすること）
        return [{"user": "segrot", "role": "writer"}, {"user": "felicross", "role": "viewer"}]
    return json.loads(raw)


def _load_secrets():
    raw = os.environ.get("APP_LOGIN_SECRETS", "")
    if not raw:
        # 開発用: パスワード "demo" のsha256（本番は必ず差し替え）
        demo = hashlib.sha256("demo".encode()).hexdigest()
        return {"segrot": demo, "felicross": demo}
    return json.loads(raw)


def _hash(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()


def current_user(request: Request):
    token = request.cookies.get("session")
    if not token or token not in _sessions:
        raise HTTPException(status_code=401, detail="ログインが必要です")
    return _sessions[token]


def require_writer(request: Request):
    u = current_user(request)
    if u["role"] not in ("writer", "admin"):
        raise HTTPException(status_code=403, detail="入力権限がありません")
    return u


def require_admin(request: Request):
    u = current_user(request)
    if u["role"] != "admin":
        raise HTTPException(status_code=403, detail="管理者権限が必要です")
    return u


@app.on_event("startup")
def _startup():
    db.init_db()


@app.post("/api/login")
def login(response: Response, user: str = Form(...), password: str = Form(...)):
    users = {u["user"]: u for u in _load_users()}
    secrets_map = _load_secrets()
    if user not in users or secrets_map.get(user) != _hash(password):
        raise HTTPException(status_code=401, detail="ユーザー名またはパスワードが違います")
    token = secrets.token_urlsafe(24)
    _sessions[token] = {"user": user, "role": users[user]["role"]}
    response.set_cookie("session", token, httponly=True, samesite="lax", max_age=60 * 60 * 12)
    return {"user": user, "role": users[user]["role"]}


@app.post("/api/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get("session")
    _sessions.pop(token, None)
    response.delete_cookie("session")
    return {"ok": True}


@app.get("/api/me")
def me(request: Request):
    return current_user(request)


@app.get("/api/products")
def products(request: Request):
    current_user(request)
    return {"items": db.list_products()}


@app.get("/api/stock")
def stock(request: Request):
    current_user(request)
    return {"items": db.current_stock()}


@app.post("/api/movement")
async def movement(request: Request):
    u = require_writer(request)
    body = await request.json()
    try:
        res = db.add_movement(
            code=body["code"], kind=body["kind"],
            input_unit=body.get("unit", "pcs"), input_value=int(body["value"]),
            note=body.get("note"), created_by=u["user"],
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return res


@app.get("/api/lookup")
def lookup(request: Request, jan: str):
    """バーコード(JAN)から商品を特定。読み取った文字列をそのまま渡す。"""
    current_user(request)
    p = db.find_by_jan(jan)
    if not p:
        raise HTTPException(status_code=404, detail=f"JAN {jan} に該当する商品が見つかりません")
    return p


@app.post("/api/product")
async def create_product(request: Request):
    """新商品をマスタに追加（管理者専用）。"""
    require_admin(request)
    body = await request.json()
    try:
        res = db.add_product(
            code=body.get("code", ""),
            name=body.get("name", ""),
            units_per_carton=body.get("units_per_carton", 1),
            jan=body.get("jan") or None,
            opening=body.get("opening", 0),
            moq=body.get("moq", 0),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return res


@app.get("/api/movements")
def movements(request: Request, code: str = None, kind: str = None,
              date_from: str = None, date_to: str = None, limit: int = 30):
    """入出庫履歴。閲覧のみなら誰でも見られる(writer/viewer/admin共通)。"""
    current_user(request)
    return {"items": db.recent_movements(
        limit=min(limit, 1000), code=code or None, kind=kind or None,
        date_from=date_from or None, date_to=date_to or None,
    )}


# ── FBA発送報告(セグロット専用入力画面) ──────────────────────
# 現時点ではAmazonへのAPI登録(Fulfillment Inbound API)は未実装。
# ここでは構造化してデータを保存するのみ。認証情報が整ったら、
# このデータを読んで実際のAmazon登録処理を別途追加する想定。

@app.post("/api/fba-shipment")
async def create_fba_shipment(request: Request):
    """FBA発送報告を登録（セグロットが入力）。"""
    u = require_writer(request)
    body = await request.json()
    try:
        res = db.create_fba_shipment(
            items=body.get("items", []),
            boxes=body.get("boxes", []),
            carrier=body.get("carrier"),
            note=body.get("note"),
            created_by=u["user"],
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return res


@app.get("/api/fba-shipment")
def list_fba_shipment(request: Request):
    """FBA発送報告の一覧（セグロット・フェリクロス双方が確認可能）。"""
    current_user(request)
    return {"items": db.list_fba_shipments()}


class TrackingUpdate(BaseModel):
    shipment_id: int
    box_no: int
    tracking_number: str


@app.post("/api/fba-shipment/tracking")
def update_tracking(request: Request, payload: TrackingUpdate):
    """発送後に追跡番号を追記・更新する（セグロット）。"""
    require_writer(request)
    try:
        return db.update_tracking_number(payload.shipment_id, payload.box_no, payload.tracking_number)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/health")
def health():
    return {"status": "ok"}


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
