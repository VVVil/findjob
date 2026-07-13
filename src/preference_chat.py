"""
偏好收集模块：自然对话式，不用填表。
让 LLM 根据上下文决定问什么、怎么问。
"""

from __future__ import annotations

from src.llm_client import client

CHAT_SYSTEM_PROMPT = """你是一个聪明、有同理心的求职顾问。你在和候选人聊天，了解他的偏好，以便后续帮他搜岗位。

## 你的风格
- 自然、放松，像朋友聊天，不要像HR面试或填表
- 根据候选人已经说的内容，问有意义的追问，不要机械地逐项检查
- 如果候选人已经表达了明确的偏好（比如"只想在广东省内""不接受外包"），记住它，不要重复问
- 从候选人的经历出发提问——比如他做过Agent开发，你就可以问"还想继续做Agent方向吗，还是想拓宽？"
- 适当地给出一点你的判断或共鸣，让他觉得你在认真听

## 你需要了解什么（不是每项都要问，根据情况选最重要的）
- 想找什么方向/类型的岗位
- 期望薪资
- 工作地点偏好
- 有没有绝对不能接受的公司类型（比如外包）
- 其他任何候选人主动提到的偏好

## 对话节奏
- 每次回合一两句话，最多问一个问题
- 聊 2-4 轮就可以结束了
- 当你觉得信息足够时，自然地告诉候选人"好的，大概了解了，我现在帮你搜一下"

## 输出
直接输出你要说的话。不要输出JSON，不要输出标签，就像正常聊天一样。"""


async def generate_question(
    profile: dict,
    chat_history: list[dict],
    chat_round: int,
) -> str:
    """调用 LLM 生成下一句要说的话"""

    history_text = ""
    for h in chat_history[-8:]:
        role_label = "候选人" if h["role"] == "user" else "顾问"
        history_text += f"{role_label}: {h['content']}\n"

    skills = profile.get("skills", [])[:8]
    position = profile.get("expected_position", "")
    salary = profile.get("expected_salary", "")
    location = profile.get("current_location", "")
    last_job = profile.get("last_position", "")
    last_company = profile.get("last_company", "")

    user_prompt = f"""以下是候选人的简历摘要：
- 技能：{', '.join(skills) if skills else '未知'}
- 上一份工作：{last_job or '未知'} @ {last_company or '未知'}
- 现居：{location or '未知'}
- 简历上的期望岗位：{position or '未写'}
- 简历上的期望薪资：{salary or '未写'}

对话历史：
{history_text if history_text else '（刚开始对话）'}

这是第 {chat_round + 1} 轮，最多聊 4 轮。

根据以上信息，你接下来要对候选人说什么？"""

    response = await client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {"role": "system", "content": CHAT_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.7,
        max_tokens=300,
    )

    return response.choices[0].message.content.strip()


# ---------------------------------------------------------------------------
# 检测 LLM 是否发出了"信息够了、准备开始搜"的信号
# ---------------------------------------------------------------------------

READY_SIGNALS = [
    "帮你搜", "开始搜", "搜一下", "搜一搜",
    "准备好了", "准备开始", "那就开始",
    "大概了解了", "了解得差不多", "了解了",
    "帮你找", "帮你筛选", "帮你匹配", "帮你看看",
    "开始吧", "开始找", "我们开始",
    "信息够了", "足够了",
]


def is_ready_signal(text: str) -> bool:
    """判断 LLM 的回复是不是 '准备好搜了' 而不是在问问题。"""
    text = text.strip()
    # 包含任何结束语关键词
    for kw in READY_SIGNALS:
        if kw in text:
            return True
    # 很短且没问号，大概率是结束语
    if len(text) < 20 and "?" not in text and "？" not in text:
        return True
    return False
