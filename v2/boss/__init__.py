# BOSS 直聘平台
from boss.scraper import scrape
from boss.sender import send_greeting, touch_job
# chat 模块暂未迁移到 BrowserSession，需要时请直接 python boss/chat.py
# from boss.chat import detect_unread, list_unread
