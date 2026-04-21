import json
import re
import asyncio
import random
from pathlib import Path
from nonebot import logger, on_command
from nonebot.adapters.onebot.v11 import Bot, MessageEvent, GroupMessageEvent, Message, MessageSegment
from nonebot.params import CommandArg
from nonebot.permission import SUPERUSER

from .event import EventTypeEnum, BeforeChatEvent, ChatEvent
from .matcher import Matcher

# 路径配置
DATA_FILE = Path("data/suggarchat/favorability.json")
STICKER_DIR = Path("data/suggarchat/stickers")

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

# --- 好感度管理 ---
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
        except Exception: return {}

    def _write_data(self, data: dict):
        try:
            with open(DATA_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
        except Exception as e: logger.error(f"写入失败: {e}")

    async def update_data(self, event: MessageEvent, change: int, new_eval: str = None) -> dict:
        async with self.lock:
            data = self._read_data()
            user_id = str(event.user_id)
            group_id = str(event.group_id) if isinstance(event, GroupMessageEvent) else "private"
            if group_id not in data: data[group_id] = {}
            if user_id not in data[group_id]: data[group_id][user_id] = {"score": 0, "eval": "初次见面"}
            data[group_id][user_id]["score"] += max(-5, min(5, change))
            if new_eval: data[group_id][user_id]["eval"] = new_eval.strip()
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
            if gid not in data: data[gid] = {}
            if uid not in data[gid]: data[gid][uid] = {"score": 0, "eval": "管理员干预"}
            data[gid][uid]["score"] = score
            self._write_data(data)

    async def reset_user(self, gid: str, uid: str):
        async with self.lock:
            data = self._read_data()
            if gid in data and uid in data[gid]:
                data[gid][uid] = {"score": 0, "eval": "记忆已被抹除"}
                self._write_data(data)

favor_db = FavorabilityManager()

# ================= Hook (注入) =================

before_matcher = Matcher(EventTypeEnum.BEFORE_CHAT, priority=1)

@before_matcher.handle()
async def _(event: BeforeChatEvent):
    nbevent = event.get_nonebot_event()
    if not isinstance(nbevent, MessageEvent): return
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

    try:
        msgs = event.get_send_message().unwrap()
        if msgs:
            target_msg = msgs[-1]
            if isinstance(target_msg.content, str): target_msg.content += favor_prompt
            elif isinstance(target_msg.content, list):
                target_msg.content.append({"type": "text", "text": favor_prompt})
    except Exception as e: logger.error(f"注入失败: {e}")

# ================= Hook (解析与跟发) =================

chat_matcher = Matcher(EventTypeEnum.CHAT, priority=1)

# 定义统一的正则常量，支持中英文冒号，允许标记内部有微量空格
RE_FAV = re.compile(r'\[FAV\s*[:：]\s*([+-]?\d+)\]', re.I)
RE_EVAL = re.compile(r'\[EVAL\s*[:：]\s*(.*?)\]', re.I)
RE_STK = re.compile(r'\[STK\s*[:：]\s*(.*?)\]', re.I)

@chat_matcher.handle()
async def _(bot: Bot, event: ChatEvent):
    response = event.model_response
    if not response: return
    
    nbevent = event.get_nonebot_event()
    if not isinstance(nbevent, MessageEvent): return

    # 1. 提取标记
    fav_match = RE_FAV.search(response)
    eval_match = RE_EVAL.search(response)
    stk_matches = RE_STK.findall(response)
    
    # 2. 彻底清理文本 (按顺序剔除所有标记)
    clean_text = RE_FAV.sub('', response)
    clean_text = RE_EVAL.sub('', clean_text)
    clean_text = RE_STK.sub('', clean_text)
    
    # 清理多余空行和前后空格
    clean_text = re.sub(r'\n\s*\n', '\n', clean_text).strip()
    
    # 关键：将清理后的文本写回 event
    event.model_response = clean_text
    
    # 3. 解析变动（如果标签不存在，则 change=0, new_eval=None）
    raw_val = int(fav_match.group(1)) if fav_match else 0
    change = max(-5, min(5, raw_val))
    new_eval = eval_match.group(1).strip() if eval_match else None
    
    # 只有在有变动时才调用数据库
    user_data = await favor_db.get_user_info(nbevent)
    if change != 0 or new_eval is not None:
        user_data = await favor_db.update_data(nbevent, change, new_eval)
    
    # 4. 异步补发详情
    async def send_extra_info():
        await asyncio.sleep(0.5)
        
        # 构造提示信息
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

        # 补发表情包
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
    
    # 根据分数给个头衔
    title = "素不相识"
    if score > 50: title = "挚友"
    elif score > 20: title = "熟人"
    elif score < -20: title = "讨厌的人"
    elif score < -50: title = "不共戴天"

    await cmd_query.finish(
        f"📊 你的好感度档案：\n"
        f"当前分数：{score} ({title})\n"
        f"她的评价：{evaluation}"
    )

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
    
    msg = "🏆 【本群好感度荣誉榜】 🏆\n"
    msg += "————————————————"
    
    medals = ["🥇", "🥈", "🥉", "👤", "👤", "👤", "👤", "👤", "👤", "👤"]
    
    for i, (item, member_info) in enumerate(zip(sorted_list, results)):
        uid, udata = item
        score = udata['score']
        last_eval = udata['eval']
        
        name = str(uid)
        if not isinstance(member_info, Exception):
            name = member_info.get("card") or member_info.get("nickname") or str(uid)
        
        # 截断过长的评价
        display_eval = (last_eval[:12] + '..') if len(last_eval) > 12 else last_eval
        
        msg += f"\n{medals[i]} {name} | {score}分\n   └ 📝 {display_eval}"
    
    msg += "\n————————————————\n💡 发送“好感度查询”查看你的详细档案"
    await cmd_rank.finish(msg)

cmd_reset_self = on_command("重置好感度", priority=5, block=True)
@cmd_reset_self.handle()
async def _(event: MessageEvent):
    # 任何用户都可以重置自己
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