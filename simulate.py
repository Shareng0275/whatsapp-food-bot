"""Run this to demo the bot end-to-end in a terminal:

    python simulate.py                # interactive
    python simulate.py --auto         # scripted walkthrough, no typing needed
"""
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from database import get_db
from repository import Repository
from conversation import Session

DEMO_PHONE = "+919900011122"  # matches the seeded user in data_store.py, has order history


def run_interactive():
    with get_db() as db:
        repo = Repository(db)
        session = Session(DEMO_PHONE, repo)
        print(session.handle_message("hi"))
        while True:
            try:
                text = input("\nYou: ")
            except (EOFError, KeyboardInterrupt):
                break
            if text.lower() in ("quit", "exit"):
                break
            print("\nBot:", session.handle_message(text))


def run_auto():
    with get_db() as db:
        repo = Repository(db)
        session = Session(DEMO_PHONE, repo)
        script = ["hi", "Veg Biryani", "1", "same", "SAVE15"]
        for msg in script:
            print(f"You: {msg}")
            print("Bot:", session.handle_message(msg))
            print()


if __name__ == "__main__":
    if "--auto" in sys.argv:
        run_auto()
    else:
        run_interactive()
