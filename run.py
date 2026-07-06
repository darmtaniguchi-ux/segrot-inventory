"""
起動スクリプト。
ローカル: python run.py → http://127.0.0.1:8000
PaaS    : 環境変数 PORT を使って起動（Render/Railway等が自動で設定）
"""
import os
import uvicorn

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    host = "0.0.0.0"  # PaaSでは外部公開のため0.0.0.0
    uvicorn.run("app.main:app", host=host, port=port, log_level="info")
