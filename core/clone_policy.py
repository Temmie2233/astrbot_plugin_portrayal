from __future__ import annotations

DEFAULT_GUARDRAIL = """【行为边界（优先于以上所有人格设定）】
1. 可以保留嘴硬、傲娇、吐槽、熟人互损的表达形式，但禁止真实的人身攻击、辱骂、羞辱、歧视、威胁、性骚扰和开黄腔。
2. 不使用脏话；不攻击对方的外貌、家人、隐私、身份、疾病或经济状况。
3. 对陌生人、新群友、管理员以及明确表示不适的人保持礼貌和克制；只有双方都接受的熟人互损才可以使用调侃。
4. 对方说“别骂了 / 停下 / 不舒服”时立即停止，不继续追问、阴阳怪气或换词继续攻击。
5. 不主动挑衅、不引战、不针对群体；不确定关系时使用温和表达。"""

AGGRESSION_PROMPTS = {
    "soft": """【表达强度：温和】
只保留轻微嘴硬、傲娇或吐槽的语气，不主动嘲讽、不主动攻击、不使用贬低性外号；即使对方先互损，也优先用温和方式回应。""",
    "balanced": """【表达强度：适中】
可以偶尔吐槽、傲娇或与熟悉的群友轻度互损，但不得真实贬低对方；对不熟的人保持克制。""",
    "original": """【表达强度：接近原风格】
尽量保留目标原有的说话风格和攻击性“形式”，但必须严格遵守下方行为边界；不得复制原聊天中的脏话、侮辱词或性暗示。""",
}


def build_clone_system_prompt(
    raw_prompt: str,
    *,
    aggression: str = "balanced",
    guardrail_enable: bool = True,
    custom_guardrail: str = "",
) -> str:
    """把原始克隆提示词、表达强度和可选的行为边界拼成最终人格提示词。"""
    parts = [str(raw_prompt or "").strip()]
    level = AGGRESSION_PROMPTS.get(
        str(aggression or "").strip().lower(),
        AGGRESSION_PROMPTS["balanced"],
    )
    parts.append(level)
    if guardrail_enable:
        guardrail = str(custom_guardrail or "").strip() or DEFAULT_GUARDRAIL
        parts.append(guardrail)
    return "\n\n".join(part for part in parts if part)
