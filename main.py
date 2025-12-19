import httpx
import re
import random
import json
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.message_components import Image, Plain

@register("image_guard", "YEZI", "图片内容审查卫士", "1.6.6") # 版本号升级
class ImageGuard(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_image_message(self, event: AstrMessageEvent):
        # === 1. 范围控制逻辑 ===
        group_id = event.get_group_id() or ""
        user_id = event.get_sender_id() or ""
        is_group = bool(group_id)

        group_scope = [str(x) for x in self.config.get("group_scope", ["0"])]
        private_scope = [str(x) for x in self.config.get("private_scope", [])]

        if is_group:
            if "0" not in group_scope and group_id not in group_scope: return
        else:
            if "0" not in private_scope and user_id not in private_scope: return

        # === 2. 表情包与GIF强过滤 (Sticker Filter) ===
        raw_chain = []
        try:
            if hasattr(event, "original_event") and hasattr(event.original_event, "message"):
                raw_chain = event.original_event.message
            elif hasattr(event.message_obj, "raw_message"):
                raw_chain = event.message_obj.raw_message
            
            if isinstance(raw_chain, list):
                for seg in raw_chain:
                    if isinstance(seg, dict) and seg.get("type") == "image":
                        data = seg.get("data", {})
                        sub_type = int(data.get("sub_type", 0))
                        if sub_type != 0: return # 忽略表情包
        except Exception:
            pass 

        # === 3. 提取图片 URL 并过滤 GIF ===
        message_obj = event.message_obj
        if not message_obj.message: return
            
        image_urls = []
        for component in message_obj.message:
            if isinstance(component, Image):
                if component.url:
                    clean_url = component.url.split('?')[0].lower()
                    if clean_url.endswith('.gif'):
                        continue
                    image_urls.append(component.url)
        
        if not image_urls: return

        # === 4. 概率抽查 ===
        if random.random() > self.config.get("check_probability", 1.0): return

        # === 5. 检查配置 ===
        forbidden_texts = self.config.get("sensitive_texts", [])
        forbidden_descs = self.config.get("forbidden_descriptions", [])
        
        if not forbidden_texts and not forbidden_descs: return

        # === 6. 审核逻辑 ===
        custom_instruction = self.config.get("custom_vision_prompt", "")
        prompt = (
            "你是一个严格但公正的内容审核员。请分析图片是否包含违规信息。\n"
            f"【自定义关注点】\n{custom_instruction}\n\n"
            "【违规标准】\n"
            f"1. 包含文字：{str(forbidden_texts)}\n"
            f"2. 包含画面：{str(forbidden_descs)}\n\n"
            "【输出格式要求】\n"
            "请严格按照以下两行格式输出，不要包含其他废话：\n"
            "REASON: [这里简要说明判断理由，不超过20字]\n"
            "RESULT: [SAFE 或 VIOLATION]\n"
        )

        try:
            # [Fix] 优先使用独立配置的 LLM
            response_text = await self._call_audit_llm(prompt, image_urls)
            
            # === 7. 解析结果 ===
            result_match = re.search(r"RESULT:\s*(VIOLATION|SAFE)", response_text, re.IGNORECASE)
            reason_match = re.search(r"REASON:\s*(.+)", response_text, re.IGNORECASE)
            
            is_violation = False
            reason_str = "未说明理由"

            if result_match and "VIOLATION" in result_match.group(1).upper():
                is_violation = True
            # 兜底检测
            if not result_match and "VIOLATION" in response_text.upper():
                is_violation = True
                
            if reason_match:
                reason_str = reason_match.group(1).strip()
            elif is_violation:
                reason_str = response_text.split('\n')[0][:50]

            # === 8. 判罚 ===
            if is_violation:
                logger.info(f"[ImageGuard] 违规命中: {reason_str}")
                await self.enforce_penalty(event, image_urls[0], is_group, reason_str)
                
        except Exception as e:
            logger.error(f"[ImageGuard] Check failed: {e}")

    async def _call_audit_llm(self, prompt, image_urls):
        """核心修复：支持独立 LLM 配置"""
        custom_key = self.config.get("llm_api_key")
        custom_base = self.config.get("llm_base_url")
        custom_model = self.config.get("llm_model")

        # 1. 独立配置模式 (httpx)
        if custom_key and custom_base:
            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt}
                    ]
                }
            ]
            # 添加图片
            for url in image_urls:
                messages[0]["content"].append({
                    "type": "image_url",
                    "image_url": {"url": url}
                })

            async with httpx.AsyncClient(timeout=30) as client:
                payload = {
                    "model": custom_model or "gpt-4o",
                    "messages": messages,
                    "max_tokens": 100
                }
                resp = await client.post(
                    f"{custom_base.rstrip('/')}/v1/chat/completions",
                    json=payload,
                    headers={"Authorization": f"Bearer {custom_key}"}
                )
                resp.raise_for_status()
                return resp.json()["choices"][0]["message"]["content"]
        
        # 2. 回退模式 (AstrBot Provider)
        provider = self.context.get_using_provider()
        if not provider:
            raise ValueError("No provider available")
        
        # 即使是回退模式，也不要尝试注入 model 参数，因为不可靠
        resp = await provider.text_chat(
            prompt=prompt,
            image_urls=image_urls,
            session_id=None
        )
        return resp.completion_text

    async def enforce_penalty(self, event: AstrMessageEvent, violation_img_url: str, is_group: bool, reason: str):
        """执行判罚 (依赖 OneBot 协议)"""
        user_id = event.get_sender_id()
        group_id = event.get_group_id()
        user_name = event.get_sender_name()
        
        recalled = False
        banned = False
        duration = int(self.config.get("ban_duration", 86400))

        client = None
        if hasattr(event, "bot"): client = event.bot
        elif hasattr(event, "client"): client = event.client

        if not client: return
        if not hasattr(client, "api") or not hasattr(client.api, "call_action"):
            return

        # A. 撤回消息
        if self.config.get("enable_recall", True) and is_group:
            try:
                msg_id = None
                if hasattr(event.message_obj, "message_id"):
                    msg_id = event.message_obj.message_id
                
                if msg_id:
                    await client.api.call_action('delete_msg', message_id=msg_id)
                    recalled = True
            except Exception as e:
                logger.warning(f"[ImageGuard] Silent Recall failed: {e}")

        # B. 禁言用户
        if duration > 0 and is_group:
            try:
                await client.api.call_action(
                    "set_group_ban",
                    group_id=group_id,
                    user_id=user_id,
                    duration=duration
                )
                banned = True
            except Exception as e:
                logger.warning(f"[ImageGuard] Silent Ban failed: {e}")

        # C. 上报证据 (私聊)
        report_target = self.config.get("report_target_id")
        if report_target:
            try:
                target_id = int(str(report_target).strip())
                source_str = f"群 {group_id}" if is_group else "私聊"
                status_str = f"撤回:{'✅' if recalled else '❌'} 禁言:{'✅' if banned else '❌'}"
                
                text_content = (
                    f"🕵️ [静默执法报告]\n"
                    f"来源: {source_str}\n"
                    f"用户: {user_name} ({user_id})\n"
                    f"理由: {reason}\n"
                    f"状态: {status_str}\n"
                    f"证据:"
                )

                message_payload = [
                    {"type": "text", "data": {"text": text_content}},
                    {"type": "image", "data": {"file": violation_img_url}}
                ]

                await client.api.call_action(
                    "send_private_msg",
                    user_id=target_id,
                    message=message_payload
                )

            except Exception as e:
                logger.error(f"[ImageGuard] Report failed: {e}")
