"""生产环境启动: python serve.py   (默认监听 0.0.0.0:8000)"""
import os

from waitress import serve

from app import app, start_background

if __name__ == "__main__":
    if app.config["SECRET_KEY"] == "dev-only-change-me":
        print("警告: 未设置 SECRET_KEY 环境变量, 生产环境请务必设置一个随机长字符串!")
    start_background()
    port = int(os.environ.get("PORT", 8000))
    print(f"服务已启动: http://0.0.0.0:{port}")
    serve(app, host="0.0.0.0", port=port, threads=8)
