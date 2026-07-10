# utils.py - 完整修复版本
import asyncio
import logging
from telegram.ext import ContextTypes
import config
from config import DELETE_DELAY
from database import db_execute, ban_user

async def send_temp(context: ContextTypes.DEFAULT_TYPE, text: str, chat_id: int, delay: int = DELETE_DELAY):
    """发送临时消息并倒计时删除"""
    logging.info(f"发送临时消息到 {chat_id}: {text[:50]}...")
    msg = await context.bot.send_message(chat_id, f"{text}\n\n⏳ 消息将在 {delay} 秒后自动删除")
    for remaining in range(delay - 1, 0, -1):
        await asyncio.sleep(1)
        try:
            await msg.edit_text(f"{text}\n\n⏳ 消息将在 {remaining} 秒后自动删除")
        except:
            break
    try:
        await msg.delete()
    except:
        pass


async def kick_user(context: ContextTypes.DEFAULT_TYPE, user_id: int, reason: str = "未通过验证", ban: bool = True):
    """踢出用户并标记封禁状态

    Args:
        ban: True=标记数据库封禁，False=只踢出不标记数据库
    """
    from database import ban_user as db_ban, unban_user, db_execute
    try:
        await context.bot.ban_chat_member(config.GROUP_ID, user_id)
        await context.bot.unban_chat_member(config.GROUP_ID, user_id)

        if ban:
            db_ban(user_id, reason)
            logging.info(f"已踢出用户 {user_id}，数据库标记封禁，原因: {reason}")
        else:
            logging.info(f"已踢出用户 {user_id}（未封禁），原因: {reason}")

        try:
            await context.bot.send_message(user_id, f"⚠️ 你已被移出群组\n原因: {reason}\n如有疑问请联系管理员。")
        except:
            pass

    except Exception as e:
        logging.error(f"踢人失败 {user_id}: {e}")


# 从 database 导入并重新导出
from database import is_user_following_channel
