"""测试 CAPTCHA Agent — 完全模拟 run.py 的详情页抓取流程"""
import asyncio, sys
sys.path.insert(0, r"D:\findjob\findjob")
from playwright.async_api import async_playwright
from src.captcha_agent import solve_captcha

PROFILE = r"D:\findjob\findjob\playwright_profile"
TEST_URL = "https://www.zhaopin.com/jobdetail/CC558104210J40502001508.htm"

async def main():
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=PROFILE, headless=False
        )

        # 只清验证码 cookie
        temp_page = await ctx.new_page()
        await temp_page.goto("https://www.zhaopin.com", wait_until="domcontentloaded", timeout=30_000)
        await ctx.clear_cookies(name="EO-Bot-Captcha-Token")
        await temp_page.close()
        print("🧹 已清除验证码 cookie\n")

        # 模拟 run.py 的方式：开新页面访问详情
        detail_page = await ctx.new_page()
        print(f"🔗 访问 {TEST_URL[:50]}...")
        try:
            await detail_page.goto(TEST_URL, wait_until="domcontentloaded", timeout=15_000)
        except Exception:
            pass
        await asyncio.sleep(2)

        # 检查是否有验证码
        has_captcha = await detail_page.evaluate(
            "() => !!document.querySelector('#tcaptcha_iframe_eo')"
        )
        if not has_captcha:
            print("✅ 无需验证，直接加载了")
        else:
            print("⚠️ 检测到验证码，CAPTCHA Agent 启动...\n")
            handled = await solve_captcha(detail_page)
            print(f"\n{'🎉 成功' if handled else '❌ 失败'}")

        print("\n30 秒后自动退出...")
        await asyncio.sleep(30)
        await ctx.close()

asyncio.run(main())
