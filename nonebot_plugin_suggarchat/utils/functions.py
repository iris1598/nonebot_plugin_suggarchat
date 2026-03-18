import json
import re
from datetime import datetime
from typing import Any

import pytz
from nonebot import logger
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    Message,
)

from nonebot_plugin_suggarchat.utils.logging import debug_log

from ..config import ConfigManager


def remove_think_tag(text: str) -> str:
    """移除第一次出现的think标签

    Args:
        text (str): 处理的参数

    Returns:
        str: 处理后的文本
    """

    start_tag = "<think>"
    end_tag = "</think>"

    # 查找第一个起始标签的位置
    start_idx = text.find(start_tag)
    if start_idx == -1:
        return text  # 没有找到起始标签，直接返回原文本

    # 在起始标签之后查找结束标签的位置
    end_idx = text.find(end_tag, start_idx + len(start_tag))
    if end_idx == -1:
        return text  # 没有找到对应的结束标签，返回原文本

    # 计算结束标签的结束位置
    end_of_end_tag = end_idx + len(end_tag)

    # 拼接移除标签后的文本
    text_new = text[:start_idx] + text[end_of_end_tag:]
    while text_new.startswith("\n"):
        text_new = text_new[1:]
    return text_new


async def is_member(event: GroupMessageEvent, bot: Bot) -> bool:
    """判断用户是否为群组普通成员"""
    # 获取群成员信息
    user_role = (
        (
            await bot.get_group_member_info(
                group_id=event.group_id, user_id=event.user_id
            )
        )["role"]
        if not event.sender.role
        else event.sender.role
    )
    return user_role == "member"


def format_datetime_timestamp(time: int) -> str:
    """将时间戳格式化为日期、星期和时间字符串"""
    now = datetime.fromtimestamp(time)
    formatted_date = now.strftime("%Y-%m-%d")
    formatted_weekday = now.strftime("%A")
    formatted_time = now.strftime("%I:%M:%S %p")
    return f"[{formatted_date} {formatted_weekday} {formatted_time}]"


# 在文件顶部预编译正则表达式
SENTENCE_DELIMITER_PATTERN = re.compile(r'([。！？!?;；\n]+)[""\'\'"\s]*', re.UNICODE)


def split_message_into_chats(text: str, max_length: int = 100) -> list[str]:
    """
    根据标点符号分割文本为句子

    Args:
        text: 要分割的文本
        max_length: 单个句子的最大长度，默认100个字符

    Returns:
        list[str]: 分割后的句子列表
    """
    if not text or not text.strip():
        return []

    sentences = []
    start = 0
    for match in SENTENCE_DELIMITER_PATTERN.finditer(text):
        end = match.end()
        if sentence := text[start:end].strip():
            sentences.append(sentence)
        start = end

    # 处理剩余部分
    if start < len(text):
        if remaining := text[start:].strip():
            sentences.append(remaining)

    # 处理过长的句子
    result = []
    for sentence in sentences:
        if len(sentence) <= max_length:
            result.append(sentence)
        else:
            # 如果句子过长且没有适当的分隔点，按最大长度切分
            chunks = [
                sentence[i : i + max_length]
                for i in range(0, len(sentence), max_length)
            ]
            result.extend(chunks)

    return result


async def synthesize_forward_message(forward_msg: dict, bot: Bot) -> str:
    """合成消息数组内容为字符串"""
    result = ""
    
    # 第一步：正确提取 messages 数组
    messages = []
    
    # 情况1：forward_msg 是包含 "messages" 键的字典
    if isinstance(forward_msg, dict) and "messages" in forward_msg:
        messages = forward_msg["messages"]
    # 情况2：forward_msg 本身就是消息数组
    elif isinstance(forward_msg, list):
        messages = forward_msg
    else:
        # 未知格式
        logger.warning(f"未知的转发消息格式: {type(forward_msg)} - {forward_msg}")
        return "<!--无法解析的转发消息格式-->"
    
    # 第二步：处理消息数组
    for segment in messages:
        try:
            # 检查 segment 是否为有效的消息节点
            if not isinstance(segment, dict):
                result += f"<!--无效的消息段: {segment}-->\n"
                continue
                
            if "type" not in segment or segment.get("type") != "node":
                result += f"<!--非节点消息段: {segment}-->\n"
                continue
                
            if "data" not in segment:
                result += f"<!--消息段缺少data字段: {segment}-->\n"
                continue
                
            # 处理 data 字段
            data = segment["data"]
            
            # 如果 data 是字符串，尝试解析为 JSON
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except Exception:
                    result += f"{data}<!--该消息段无法被解析-->\n"
                    continue
            
            # 确保 data 是字典
            if not isinstance(data, dict):
                result += f"<!--data字段格式不正确: {data}-->\n"
                continue
                
            # 获取用户信息
            nickname = data.get("nickname", "未知用户")
            user_id = data.get("user_id", "未知QQ")
            result += f"[{nickname}({user_id})]说："
            
            # 处理消息内容
            content = data.get("content", "")
            
            if isinstance(content, str):
                # 如果 content 是字符串，直接使用
                result += f"{content}"
            elif isinstance(content, list):
                # 如果 content 是消息段数组，递归解析
                for msg_segment in content:
                    if not isinstance(msg_segment, dict):
                        continue
                        
                    segment_type = msg_segment.get("type", "")
                    segment_data = msg_segment.get("data", {})
                    
                    match segment_type:
                        case "text":
                            result += f"{segment_data.get('text', '')}"
                        case "at":
                            result += f" [@{segment_data.get('qq', '')}]"
                        case "face":
                            result += f"[表情:{segment_data.get('id', '')}]"
                        case "image":
                            result += f"[图片:{segment_data.get('file', '')}]"
                        case "record":
                            result += f"[语音:{segment_data.get('file', '')}]"
                        case "forward":
                            if "id" in segment_data:
                                try:
                                    nested_forward = await bot.get_forward_msg(id=segment_data["id"])
                                    result += f"\\（嵌套转发:{await synthesize_forward_message(nested_forward, bot)}）\\"
                                except Exception as e:
                                    result += f"\\（转发消息获取失败:{e}）\\"
                        case _:
                            result += f"[{segment_type}消息]"
            else:
                result += f"<!--未知内容格式: {type(content).__name__}-->"
                
        except Exception as e:
            logger.opt(colors=True, exception=e).warning(f"解析消息段时出错：{e!s}")
            result += f"\n<!--该消息段无法被解析--><origin>{segment!s}</origin>"
        
        result += "\n"
    
    return result.strip()


async def synthesize_message(message: Message, bot: Bot) -> str:
    """合成消息内容为字符串"""
    content = ""
    for segment in message:
        if segment.type == "text":
            content += segment.data["text"]
        elif segment.type == "at":
            content += f"\\（at: @{segment.data.get('name')}(QQ:{segment.data['qq']}))"
        elif (
            segment.type == "forward"
            and ConfigManager().config.function.synthesize_forward_message
        ):
            forward: dict[str, Any] = await bot.get_forward_msg(id=segment.data["id"])
            debug_log(forward)
            content += (
                " \\（合并转发\n"
                + await synthesize_forward_message(forward, bot)
                + "）\\\n"
            )
    return content


def split_list(lst: list, threshold: int) -> list[Any]:
    """将列表分割为多个子列表，每个子列表长度不超过阈值"""
    if len(lst) <= threshold:
        return [lst]
    return [lst[i : i + threshold] for i in range(0, len(lst), threshold)]


def get_current_datetime_timestamp():
    """获取当前时间并格式化为日期、星期和时间字符串"""
    utc_time = datetime.now(pytz.utc)
    asia_shanghai = pytz.timezone("Asia/Shanghai")
    now = utc_time.astimezone(asia_shanghai)
    formatted_date = now.strftime("%Y-%m-%d")
    formatted_weekday = now.strftime("%A")
    formatted_time = now.strftime("%H:%M:%S")
    return f"[{formatted_date} {formatted_weekday} {formatted_time}]"


async def get_friend_name(qq_number: int, bot: Bot) -> str:
    """获取好友昵称"""
    friend_list = await bot.get_friend_list()
    return next(
        (
            friend["nickname"]
            for friend in friend_list
            if friend["user_id"] == qq_number
        ),
        "",
    )
