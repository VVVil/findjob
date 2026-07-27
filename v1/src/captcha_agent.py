"""
CAPTCHA 子 Agent：将验证码区域 DOM 喂给 LLM 自主推理点击目标。
借鉴 browser-use：提取可交互元素 → 缩进文本树 → LLM 推理选择器 → 点击。
LLM 只输出 CSS 选择器，代码执行点击（零越权风险）。
命中过的选择器缓存，后续秒过。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path

from playwright.async_api import Page

HERE = Path(__file__).parent.parent
CACHE_FILE = HERE / "data" / "captcha_cache.json"

# ── selector 缓存 ──

def _load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}

def _save_cache(cache: dict) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")

# ── 内置 selector 快车道 ──

FAST_SELECTORS = [
    "#verifyCheckbox",           # EdgeOne 复选框
    ".checkbox-verify",          # EdgeOne 备用
    "#tcaptcha_drag_button",     # 滑块按钮
    "[role='checkbox']",         # 通用复选框
]

# ── DOM 提取 JS ──

EXTRACT_JS = """() => {
    function getClass(el) {
        // SVG 元素 className 是 SVGAnimatedString，不是字符串
        if (typeof el.className === 'string') return el.className;
        if (el.className && el.className.baseVal) return el.className.baseVal;
        return el.getAttribute('class') || '';
    }
    function getAttrs(el) {
        const attrs = {};
        if (el.id) attrs.id = el.id;
        const cls = getClass(el);
        if (cls) attrs.class = cls;
        if (el.type) attrs.type = el.type;
        if (el.getAttribute('aria-label')) attrs['aria-label'] = el.getAttribute('aria-label');
        if (el.getAttribute('role')) attrs.role = el.getAttribute('role');
        if (el.onclick) attrs.onclick = '(has onclick)';
        return attrs;
    }
    const INTERACTIVE_TAGS = new Set(['button','input','select','textarea','a','iframe','frame']);
    const CLICKABLE_KW = ['checkbox','verify','captcha','submit','btn','button'];
    function isClickable(el) {
        const tag = el.tagName.toLowerCase();
        if (INTERACTIVE_TAGS.has(tag)) return true;
        const cls = getClass(el).toLowerCase();
        for (const kw of CLICKABLE_KW)
            if (cls.includes(kw)) return true;
        const role = el.getAttribute('role');
        if (role === 'button' || role === 'checkbox') return true;
        if (el.onclick) return true;
        return false;
    }
    function getDirectText(el) {
        let text = '';
        for (const node of el.childNodes)
            if (node.nodeType === 3) text += node.textContent;
        return text.trim().slice(0, 60);
    }
    const elements = [];
    let index = 0;
    function walk(el, depth) {
        if (!el || el.nodeType !== 1) return;
        const clickable = isClickable(el);
        const text = getDirectText(el);
        if (clickable || text) {
            elements.push({ idx: ++index, depth: depth, tag: el.tagName.toLowerCase(),
                attrs: getAttrs(el), text: text, clickable: clickable });
        }
        for (const child of el.children)
            walk(child, clickable ? depth + 1 : depth);
    }
    // 只提取验证码区域
    const root = document.querySelector('#captcha, [class*="captcha"], [class*="verify"], [class*="tcaptcha"]') || document.body;
    walk(root, 0);
    return elements;
}"""


async def _extract_iframe_dom(page: Page, iframe_sel: str) -> list[dict]:
    """钻进 iframe 提取内部 DOM"""
    try:
        for f in page.frames:
            if f == page.main_frame:
                continue
            # 匹配方式：先试 URL 特征（快），再试 frame_element（准）
            is_match = False
            url = f.url
            # EdgeOne: captcha.eo.gtimg.com / tcaptcha
            if "captcha" in url or "tcaptcha" in url or "verify" in url:
                is_match = True
            else:
                # 不是常见验证码 URL，尝试精确匹配 iframe 选择器
                try:
                    frame_el = await f.frame_element()
                    is_match = await frame_el.evaluate(f"el => el.matches('{iframe_sel}')")
                except Exception:
                    continue

            if not is_match:
                continue

            # 找到了 → 提取 DOM
            try:
                result = await f.evaluate(EXTRACT_JS)
                return result
            except Exception as e:
                print(f"  [dim]iframe evaluate 失败: {e}[/dim]")
                continue

        return []
    except Exception:
        return []


# ── 格式化 ──

def _fmt_el(el: dict) -> str:
    """格式化单个元素为文本 (借鉴 browser-use)"""
    tag = el.get("tag", "")
    attrs = el.get("attrs", {})
    pid = f"#{attrs['id']}" if attrs.get("id") else ""
    cls = ""
    if attrs.get("class"):
        classes = attrs["class"].split()[:2]
        cls = "." + ".".join(classes)
    typ = f'[type="{attrs["type"]}"]' if attrs.get("type") else ""
    text = f' "{el["text"]}"' if el.get("text") else ""
    return f"[{el['idx']}] {tag}{pid}{cls}{typ}{text}"


async def extract_captcha_dom(page: Page) -> str:
    """提取验证码区域 DOM，含 iframe 内部，输出 LLM 可读的缩进文本树"""
    # 1. 主页提取
    elements = await page.evaluate(EXTRACT_JS)

    # 2. 钻进 iframe 提取内部
    for i, el in enumerate(elements):
        if el.get("tag") == "iframe":
            el_id = el["attrs"].get("id", "")
            sel = f"#{el_id}" if el_id else f"iframe[src*=\"{el['attrs'].get('src','')[:30]}\"]"
            print(f"  [dim]  → 提取 iframe 内部: {sel}[/dim]")
            inner = await _extract_iframe_dom(page, sel)
            print(f"  [dim]  → iframe 内找到 {len(inner)} 个元素[/dim]")
            for inner_el in inner:
                inner_el["idx"] += len(elements)
                inner_el["depth"] += el["depth"] + 1
                inner_el["_in_iframe"] = True
                inner_el["_iframe_sel"] = sel
            elements.extend(inner)

    if not elements:
        return "(页面无交互元素)"

    # 3. 格式化
    lines = []
    for el in elements:
        indent = "  " * el["depth"]
        marker = "> " if el.get("clickable") else "  "
        iframe_tag = ""
        if el.get("_in_iframe"):
            iframe_tag = " [iframe内]"
        lines.append(f"{indent}{marker}{_fmt_el(el)}{iframe_tag}")

    return "\n".join(lines)


# ── LLM 推理 ──

async def ask_llm_where_to_click(dom_tree: str) -> str | None:
    """DOM 树发给 DeepSeek，返回 CSS 选择器"""
    from src.llm_client import captcha_client, CAPTCHA_MODEL

    prompt = f"""你是网页验证码分析专家。下面是一个包含人机验证的页面 DOM 结构。

找出用户应该**点击哪个元素**才能通过验证（通常是复选框或按钮）。

【页面DOM】
{dom_tree}

【规则】
1. 看元素的标签、class、文字，找出验证码交互元素
2. 如果有 id（如 #verifyCheckbox），用它；否则用 class
3. 只回复一个 CSS 选择器，不要其他内容"""

    try:
        resp = await captcha_client.chat.completions.create(
            model=CAPTCHA_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=100,
        )
        raw = resp.choices[0].message.content
        print(f"  [dim]LLM 原始返回: {raw[:200]}[/dim]")
        selector = raw.strip().split("\n")[-1].strip("`'\".。， ")
        if not selector or len(selector) > 100:
            selector = raw.strip().split("\n")[0].strip("`'\".。， ")
        if not selector or len(selector) > 100:
            print(f"  [red]未能从回复中提取选择器[/red]")
            return None
        return selector
    except Exception as e:
        print(f"  [red]LLM 调用失败: {e}[/red]")
        return None


# ── 主入口 ──

async def solve_captcha(page: Page) -> bool:
    """
    CAPTCHA 子 Agent：
    1. 缓存 + 内置快车道命中 → 秒过
    2. 提取 DOM → LLM 推理 → 点击 → 缓存
    """
    cache = _load_cache()

    # ── 快车道：缓存 + 内置选择器 ──
    for sel in list(cache.values()) + FAST_SELECTORS:
        for ctx_desc, locator in [
            ("主页", page.locator(sel)),
            ("#tcaptcha_iframe_eo", page.frame_locator("#tcaptcha_iframe_eo").locator(sel)),
        ]:
            try:
                target = locator.first
                if await target.count() > 0:
                    await target.click(timeout=3000)
                    print(f"  ⚡ 快车道: {ctx_desc}.{sel}")
                    # 等验证完成（页面可能跳转，evaluate 会抛异常，正常）
                    for _ in range(20):
                        await asyncio.sleep(1)
                        try:
                            still = await page.evaluate(
                                "() => !!document.querySelector('#tcaptcha_iframe_eo')"
                            )
                            if not still:
                                print(f"  ✅ 验证码已消失")
                                return True
                        except Exception:
                            # 页面跳转了 → 验证码当然没了
                            print(f"  ✅ 页面已跳转，验证通过")
                            return True
                    return True  # 点了就算
            except Exception:
                continue

    # ── LLM 推理 ──
    dom_tree = await extract_captcha_dom(page)
    if dom_tree == "(页面无交互元素)":
        print("  [dim]CAPTCHA Agent: 未找到可交互元素[/dim]")
        return False

    print(f"  🤖 CAPTCHA Agent 分析中...")
    for line in dom_tree.split("\n")[:12]:
        print(f"  [dim]│ {line}[/dim]")
    if dom_tree.count("\n") > 12:
        print(f"  [dim]│ ... ({dom_tree.count(chr(10)) + 1} 行)[/dim]")

    selector = await ask_llm_where_to_click(dom_tree)
    if not selector:
        print("  [red]LLM 未能推理出点击目标[/red]")
        return False

    print(f"  🎯 LLM 推理: {selector}")

    # ── 执行点击（主页 + 已知 iframe）──
    iframe_ctxs = [
        ("主页", page.locator(selector)),
        ("#tcaptcha_iframe_eo", page.frame_locator("#tcaptcha_iframe_eo").locator(selector)),
        ("EO iframe", page.frame_locator('iframe[src*="captcha.eo"]').locator(selector)),
        ("tcaptcha iframe", page.frame_locator('iframe[src*="tcaptcha"]').locator(selector)),
    ]
    for ctx_desc, locator in iframe_ctxs:
        try:
            target = locator.first
            if await target.count() > 0:
                await target.click(timeout=5000)
                print(f"  ✅ 已点击 {ctx_desc}.{selector}")

                # 等验证完成：检查 iframe 是否消失 / 页面是否跳转
                for _ in range(20):  # 最多等 20 秒
                    await asyncio.sleep(1)
                    # 检查验证码是否还在
                    still_there = await page.evaluate(
                        "() => !!document.querySelector('#tcaptcha_iframe_eo')"
                    )
                    if not still_there:
                        print(f"  ✅ 验证码已消失，验证成功")
                        break
                else:
                    print(f"  [yellow]验证码仍在，但已点击（可能需人工确认）[/yellow]")

                # 缓存
                key = selector.replace("#", "").replace(".", "_")[:40]
                cache[key] = selector
                _save_cache(cache)
                print(f"  💾 已缓存")
                return True
        except Exception:
            continue

    print(f"  [red]选择器在所有上下文中均未匹配: {selector}[/red]")
    return False
