# extra.py - 严格遵循 nonebot-plugin-chatrecorder + uninfo 官方文档

import json
import re
import asyncio
import random
import httpx
from datetime import datetime, timedelta, timezone
from pathlib import Path

from nonebot import logger, require, on_command
from nonebot.adapters.onebot.v11 import Bot, MessageEvent, GroupMessageEvent, Message, MessageSegment
from nonebot.params import CommandArg
from nonebot.permission import SUPERUSER

require("nonebot_plugin_chatrecorder")
require("nonebot_plugin_uninfo")

from nonebot_plugin_uninfo import get_session

from .event import EventTypeEnum, BeforeChatEvent, ChatEvent
from .matcher import Matcher

# ================= 配置区 =================
SUMMARY_CONFIG = {
    "api_url": "",
    "api_key": "",  # 必填
    "model": "",  # 推荐 7B/14B 级别
    "timeout": 15,
    "max_history_minutes": 60,
    "max_web_chars": 600
}

# ================= 路径配置 =================
DATA_FILE = Path("data/suggarchat/favorability.json")
STICKER_DIR = Path("data/suggarchat/stickers")

# ================= 表情包管理 =================
class StickerManager:
    @staticmethod
    def get_categories() -> list[str]:
        if not STICKER_DIR.exists():
            STICKER_DIR.mkdir(parents=True, exist_ok=True)
            return []
        return [d.name for d in STICKER_DIR.iterdir() if d.is_dir()]

    @staticmethod
    def get_random_sticker_path(category: str) -> Path | None:
        cat_path = STICKER_DIR / category
        if not cat_path.exists() or not cat_path.is_dir():
            return None
        files = [f for f in cat_path.iterdir() if f.is_file() and f.suffix.lower() in ('.jpg', '.png', '.gif')]
        if not files:
            return None
        return random.choice(files).absolute()

# ================= 好感度管理 =================
class FavorabilityManager:
    _instance = None
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(FavorabilityManager, cls).__new__(cls)
            cls._instance.lock = asyncio.Lock()
            cls._instance._ensure_file()
        return cls._instance

    def _ensure_file(self):
        if not DATA_FILE.exists():
            DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(DATA_FILE, "w", encoding="utf-8") as f:
                json.dump({}, f)

    def _read_data(self) -> dict:
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception: 
            return {}

    def _write_data(self, data: dict):
        try:
            with open(DATA_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
        except Exception as e: 
            logger.error(f"写入失败: {e}")

    async def update_data(self, event: MessageEvent, change: int, new_eval: str = None) -> dict:
        async with self.lock:
            data = self._read_data()
            user_id = str(event.user_id)
            group_id = str(event.group_id) if isinstance(event, GroupMessageEvent) else "private"
            if group_id not in data:
                data[group_id] = {}
            if user_id not in data[group_id]: 
                data[group_id][user_id] = {"score": 0, "eval": "初次见面"}
            data[group_id][user_id]["score"] += max(-5, min(5, change))
            if new_eval: 
                data[group_id][user_id]["eval"] = new_eval.strip()
            self._write_data(data)
            return data[group_id][user_id]

    def _get_keys(self, event: MessageEvent) -> tuple[str, str]:
        user_id = str(event.user_id)
        group_id = str(event.group_id) if isinstance(event, GroupMessageEvent) else "private"
        return group_id, user_id

    async def get_user_info(self, event: MessageEvent) -> dict:
        data = self._read_data()
        user_id = str(event.user_id)
        group_id = str(event.group_id) if isinstance(event, GroupMessageEvent) else "private"
        return data.get(group_id, {}).get(user_id, {"score": 0, "eval": "暂无评价"})

    async def set_score(self, gid: str, uid: str, score: int):
        async with self.lock:
            data = self._read_data()
            if gid not in data:
                data[gid] = {}
            if uid not in data[gid]: 
                data[gid][uid] = {"score": 0, "eval": "管理员干预"}
            data[gid][uid]["score"] = score
            self._write_data(data)

    async def reset_user(self, gid: str, uid: str):
        async with self.lock:
            data = self._read_data()
            if gid in data and uid in data[gid]:
                data[gid][uid] = {"score": 0, "eval": "记忆已被抹除"}
                self._write_data(data)

favor_db = FavorabilityManager()

# ================= 上下文摘要模块 (严格遵循官方文档) =================
class ContextSummaryManager:
    def __init__(self):
        self.cfg = SUMMARY_CONFIG

    async def _call_ai(self, system_prompt: str, user_prompt: str) -> str:
        try:
            payload = {
                "model": self.cfg["model"],
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                "temperature": 0.3,
                "max_tokens": 300,
                "enable_thinking": False
            }
            headers = {
                "Authorization": f"Bearer {self.cfg['api_key']}",
                "Content-Type": "application/json"
            }
            async with httpx.AsyncClient(timeout=self.cfg["timeout"]) as client:
                resp = await client.post(self.cfg["api_url"], json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                content = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
                if not content:
                    raise ValueError("AI 返回内容为空")
                return content
        except httpx.HTTPStatusError as e:
            logger.error(f"[摘要模块] HTTP {e.response.status_code}: {e.response.text[:200]}")
            raise
        except Exception as e:
            logger.error(f"[摘要模块] AI 请求异常: {type(e).__name__}: {e}")
            raise

    async def summarize_history(self, bot: Bot, nbevent: MessageEvent) -> str:
        """独立流程 1：总结聊天记录（严格遵循官方文档）"""
        logger.info("[摘要模块] 开始拉取并总结聊天记录...")
        try:
            from nonebot_plugin_chatrecorder import get_messages_plain_text
            
            # 🔥 关键修复：await get_session(bot, event) - 参数顺序和异步调用
            session = await get_session(bot, nbevent)
            if not session:
                logger.warning("[摘要模块] 无法获取 session 对象，跳过历史总结")
                return "无法获取会话上下文"
            
            # 计算时间范围
            time_start = datetime.now(timezone.utc) - timedelta(minutes=self.cfg["max_history_minutes"])
            
            # 🔥 关键修复：使用 session + filter_scene 筛选同一会话，移除无效参数
            msgs = await get_messages_plain_text(
                session=session,           # 🔥 必须传入 Session 对象
                filter_scene=True,         # 🔥 筛选同一场景（同群/同私聊）
                filter_user=False,         # 获取所有成员消息（不筛选特定用户）
                time_start=time_start,     # 时间范围
                types=["message"]          # 只获取文本消息类型
                # ❌ 移除 exclude_bot 等无效参数
            )
            
            if not msgs:
                logger.debug("[摘要模块] 无有效文本记录可总结")
                return "暂无近期文本对话"
            
            # 拼接为聊天记录格式（最多取最近 20 条）
            chat_text = "\n".join(msgs[-20:])
            
            summary = await self._call_ai(
                system_prompt="你是一个上下文提炼助手。请用不超过 80 字的中文，客观总结以下聊天记录的核心话题。若无实质内容请回复'无'。",
                user_prompt=f"聊天记录（最近 {self.cfg['max_history_minutes']} 分钟）：\n{chat_text}"
            )
            return summary if summary != "无" else "近期对话无有效信息"
            
        except ImportError as e:
            logger.error(f"[摘要模块] 依赖导入失败: {e}\n请确认: 1) 已安装 nonebot-plugin-chatrecorder>=0.5  2) 已运行 'nb orm upgrade'")
            raise
        except Exception as e:
            logger.error(f"[摘要模块] 历史记录总结失败: {type(e).__name__}: {e}")
            raise

    async def summarize_web(self, urls: list[str]) -> str:
        """独立流程 2：总结网页内容"""
        if not urls:
            return "当前消息无网页链接"
            
        logger.info(f"[摘要模块] 发现 {len(urls)} 个链接，准备抓取总结...")
        web_contents = []
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        
        async with httpx.AsyncClient(
            follow_redirects=True, 
            timeout=12, 
            headers=headers,
            verify=False
        ) as client:
            for i, url in enumerate(urls[:2]):
                try:
                    logger.debug(f"[摘要模块] 正在抓取: {url}")
                    
                    for retry in range(3):
                        try:
                            resp = await client.get(url)
                            break
                        except httpx.ConnectError:
                            if retry == 2:
                                raise
                            logger.warning(f"[摘要模块] {url} 连接失败，重试 {retry+1}/2...")
                            await asyncio.sleep(1)
                    
                    if resp.status_code != 200:
                        logger.warning(f"[摘要模块] {url} 返回 {resp.status_code}")
                        continue
                        
                    # httpx 编码处理
                    content_type = resp.headers.get("content-type", "").lower()
                    if "charset=" in content_type:
                        encoding = content_type.split("charset=")[-1].split(";")[0].strip()
                    elif resp.encoding:
                        encoding = resp.encoding
                    else:
                        encoding = "utf-8"
                    
                    try:
                        text = resp.content.decode(encoding, errors="ignore")
                    except LookupError:
                        text = resp.text
                    
                    text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL | re.IGNORECASE)
                    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
                    text = re.sub(r'<[^>]+>', ' ', text)
                    text = re.sub(r'[\n\r\t]+', ' ', text)
                    text = re.sub(r'\s{2,}', ' ', text).strip()
                    
                    if len(text) < 50:
                        logger.warning(f"[摘要模块] {url} 提取内容过短 ({len(text)} 字符)")
                        continue
                        
                    web_contents.append(f"[链接 {i+1}] {text[:self.cfg['max_web_chars']]}")
                    logger.debug(f"[摘要模块] 成功提取 {url} ({len(text)} 字符)")
                    break
                    
                except httpx.ConnectTimeout:
                    logger.warning(f"[摘要模块] 连接超时: {url}")
                except httpx.ReadTimeout:
                    logger.warning(f"[摘要模块] 读取超时: {url}")
                except httpx.HTTPStatusError as e:
                    logger.warning(f"[摘要模块] HTTP 错误 {e.response.status_code} for {url}")
                except httpx.ConnectError as e:
                    logger.warning(f"[摘要模块] 连接错误 {url}: {e}")
                except Exception as e:
                    logger.warning(f"[摘要模块] 抓取 {url} 异常: {type(e).__name__}: {e}")
                    
        if not web_contents:
            return "网页内容无法提取（可能被反爬/需登录/动态加载）"
            
        summary = await self._call_ai(
            system_prompt="你是一个网页摘要助手。请提取以下网页的核心信息，用不超过 100 字的中文简明概括。",
            user_prompt="\n\n".join(web_contents)
        )
        return summary if summary != "无" else "网页内容无法提取有效信息"

summary_mgr = ContextSummaryManager()

# ================= Hook (注入) =================
before_matcher = Matcher(EventTypeEnum.BEFORE_CHAT, priority=1)

# 🔥 关键修复：handler 必须接收 bot 参数，才能调用 get_session(bot, event)
@before_matcher.handle()
async def _(bot: Bot, event: BeforeChatEvent):  # 🔥 添加 bot 参数
    nbevent = event.get_nonebot_event()
    if not isinstance(nbevent, MessageEvent): 
        return
    
    user_info = await favor_db.get_user_info(nbevent)
    categories = StickerManager.get_categories()
    
    favor_prompt = (
        f"\n[系统插件指令（对用户不可见）：\n"
        f"1. 记忆状态：用户当前好感度 {user_info['score']}，你对他的评价是：{user_info['eval']}。\n"
        f"2. 自由决策：你可以根据对话内容自由决定是否更新好感度或评价。**若不需要更新，则不输出下方标记**。\n"
        f"3. 标记格式（仅在需要更新时置于回复末尾）：\n"
        f"   - [FAV:±数值]：改变好感度（范围 -5 到 +5）。\n"
        f"   - [EVAL:新评价]：如果你对用户的看法发生了改变，请输出此标记更新评价（限制20个字以内）。\n"
        f"   - [STK:分类名]：发送表情包，可选类别：{', '.join(categories)}。]\n"
    )

    chat_summary_text = None
    try:
        # 🔥 关键修复：传入 bot + nbevent 给 summarize_history
        chat_summary_text = await summary_mgr.summarize_history(bot, nbevent)
        logger.info(f"[摘要模块] 历史总结成功: {chat_summary_text[:30]}...")
    except Exception as e:
        logger.warning(f"[摘要模块] 历史总结降级: {type(e).__name__}")

    web_summary_text = None
    current_msg_text = str(nbevent.get_message())
    urls = re.findall(r'https?://[^\s<>"\']+', current_msg_text)
    if urls:
        logger.info(f"[摘要模块] 检测到链接: {urls}")
        try:
            web_summary_text = await summary_mgr.summarize_web(urls)
            logger.info(f"[摘要模块] 网页总结成功: {web_summary_text[:30]}...")
        except Exception as e:
            logger.warning(f"[摘要模块] 网页总结降级: {type(e).__name__}")

    ctx_parts = []
    if chat_summary_text:
        ctx_parts.append(f"📜 近期对话摘要: {chat_summary_text}")
    if web_summary_text and web_summary_text not in ("当前消息无网页链接", "网页内容无法提取"):
        ctx_parts.append(f"🌐 网页摘要: {web_summary_text}")

    combined_prompt = favor_prompt
    if ctx_parts:
        summary_block = "\n[系统上下文摘要（对用户不可见）：\n" + "\n".join(ctx_parts) + "\n]"
        combined_prompt = summary_block + "\n" + favor_prompt
        logger.info("[摘要模块] ✅ 成功注入上下文摘要")
    else:
        logger.info("[摘要模块] ⚠️ 摘要不可用，仅注入好感度提示词")

    try:
        msgs = event.get_send_message().unwrap()
        if msgs:
            target_msg = msgs[-1]
            if isinstance(target_msg.content, str):
                target_msg.content += combined_prompt
            elif isinstance(target_msg.content, list):
                target_msg.content.append({"type": "text", "text": combined_prompt})
    except Exception as e: 
        logger.error(f"注入失败: {e}")

# ================= Hook (解析与跟发) =================
chat_matcher = Matcher(EventTypeEnum.CHAT, priority=1)

@chat_matcher.handle()
async def _(bot: Bot, event: ChatEvent):
    response = event.model_response
    nbevent = event.get_nonebot_event()
    if not isinstance(nbevent, MessageEvent): 
        return

    fav_match = re.search(r'\[FAV[:：]([+-]?\d+)\]', response)
    eval_match = re.search(r'\[EVAL[:：](.*?)\]', response)
    stk_matches = re.findall(r'\[STK[:：](.*?)\]', response)
    
    clean_text = re.sub(r'\[FAV:[+-]?\d+\]', '', response)
    clean_text = re.sub(r'\[EVAL:.*?\]', '', clean_text)
    clean_text = re.sub(r'\[STK:.*?\]', '', clean_text).strip()
    clean_text = re.sub(r'\n{2,}', '\n', clean_text).strip()
    event.model_response = clean_text  
    
    raw_val = int(fav_match.group(1)) if fav_match else 0
    change = max(-5, min(5, raw_val))
    new_eval = eval_match.group(1).strip() if eval_match else None
    
    user_data = await favor_db.get_user_info(nbevent)
    if change != 0 or new_eval is not None:
        user_data = await favor_db.update_data(nbevent, change, new_eval)
    
    async def send_extra_info():
        await asyncio.sleep(0.5)
        tips_parts = []
        if change != 0:
            symbol = "+" if change > 0 else ""
            tips_parts.append(f"好感度 {symbol}{change} (当前: {user_data['score']})")
        if new_eval is not None:
            tips_parts.append("评价已更新 ✨")
        if tips_parts:
            try: 
                await bot.send(event=nbevent, message=" | ".join(tips_parts))
            except Exception as e: 
                logger.error(f"提示发送失败: {e}")

        if stk_matches:
            for cat in stk_matches:
                img_path = StickerManager.get_random_sticker_path(cat)
                if img_path:
                    try: 
                        await bot.send(event=nbevent, message=MessageSegment.image(img_path))
                    except Exception as e: 
                        logger.error(f"补发表情包失败: {e}")
    
    asyncio.create_task(send_extra_info())

# ================= 指令部分 =================
cmd_query = on_command("好感度查询", aliases={"我的好感度"}, priority=5, block=True)
@cmd_query.handle()
async def _(event: MessageEvent):
    info = await favor_db.get_user_info(event)
    score = info['score']
    evaluation = info['eval']
    title = "素不相识"
    if score > 50: title = "挚友"
    elif score > 20: title = "熟人"
    elif score < -20: title = "讨厌的人"
    elif score < -50: title = "不共戴天"
    await cmd_query.finish(f"📊 你的好感度档案：\n当前分数：{score} ({title})\n她的评价：{evaluation}")

cmd_rank = on_command("好感度排行", priority=5, block=True)
@cmd_rank.handle()
async def _(bot: Bot, event: GroupMessageEvent):
    data = favor_db._read_data()
    gid = str(event.group_id)
    group_data = data.get(gid, {})
    
    if not group_data:
        await cmd_rank.finish("🌸 本群还没有好感度记录哦~")
    
    sorted_list = sorted(group_data.items(), key=lambda x: x[1]['score'], reverse=True)[:10]
    tasks = [bot.get_group_member_info(group_id=event.group_id, user_id=int(uid)) for uid, _ in sorted_list]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    msg = "🏆 【本群好感度荣誉榜】 🏆\n————————————————"
    medals = ["🥇", "🥈", "🥉"] + ["👤"] * 7
    
    for i, (item, member_info) in enumerate(zip(sorted_list, results)):
        uid, udata = item
        score = udata['score']
        name = str(uid)
        if not isinstance(member_info, Exception):
            name = member_info.get("card") or member_info.get("nickname") or str(uid)
        display_eval = (udata['eval'][:12] + '..') if len(udata['eval']) > 12 else udata['eval']
        msg += f"\n{medals[i]} {name} | {score}分\n   └ 📝 {display_eval}"
    
    msg += "\n————————————————\n💡 发送'好感度查询'查看你的详细档案"
    await cmd_rank.finish(msg)

cmd_reset_self = on_command("重置好感度", priority=5, block=True)
@cmd_reset_self.handle()
async def _(event: MessageEvent):
    gid, uid = favor_db._get_keys(event)
    await favor_db.reset_user(gid, uid)
    await cmd_reset_self.finish("✨ 记忆已重置，现在的你对我来说就像一张白纸。")

cmd_admin_set = on_command("设置好感度", permission=SUPERUSER, priority=5)
@cmd_admin_set.handle()
async def _(event: GroupMessageEvent, args: Message = CommandArg()):
    score_val = args.extract_plain_text().strip()
    target_uid = next((str(seg.data['qq']) for seg in args if seg.type == "at"), None)
    if target_uid and (score_val.isdigit() or (score_val.startswith('-') and score_val[1:].isdigit())):
        await favor_db.set_score(str(event.group_id), target_uid, int(score_val))
        await cmd_admin_set.finish(f"✅ 已强制修改用户 {target_uid} 的好感度为 {score_val}")