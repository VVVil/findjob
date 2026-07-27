"""清掉验证码 cookie，保留登录态，然后直接跑 run.py 就能触发验证码"""
import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=r"D:\findjob\findjob\playwright_profile", headless=False
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://www.zhaopin.com", wait_until="domcontentloaded", timeout=30_000)
        await ctx.clear_cookies(name="EO-Bot-Captcha-Token")
        print("✅ 已清除验证码 cookie，登录态保留")
        await ctx.close()

asyncio.run(main())
