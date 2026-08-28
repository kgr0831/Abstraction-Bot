"""봇과 계정 등록기를 한 프로세스에서 띄운다.

터미널을 닫으면 이 프로세스가 죽고 등록기도 같이 내려간다 (데몬 스레드).
"""

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "app"))

import uvicorn  # noqa: E402

import bot  # noqa: E402
import launcher  # noqa: E402


def serve_launcher():
    uvicorn.run(
        launcher.app, host=launcher.HOST, port=launcher.PORT, log_level="warning"
    )


if __name__ == "__main__":
    threading.Thread(target=serve_launcher, daemon=True).start()
    print(f"콘솔  http://127.0.0.1:{launcher.PORT}  (bind {launcher.HOST})")
    print("종료하려면 이 창을 닫거나 Ctrl+C\n")
    bot.main()
