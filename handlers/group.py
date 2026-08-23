import logging
import asyncio
from telegram import Update
from telegram.ext import ContextTypes

import config
from config import DELETE_DELAY
from database import get_user, is_admin, db_execute
from utils import send_temp, kick_user

async def new_member_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """当有新用户加入群组时，立即检查资格，无资格立即踢出"""
    if update.effective_chat.id != config.GROUP_ID:
        return

    try:
        await update.message.delete()
    except Exception as e:
        logging.warning(f"删除系统消息失败: {e}")

    new_members = update.message.new_chat_members

    for member in new_members:
        user_id = member.id

        if user_id == context.bot.id:
            continue

        if is_admin(user_id):
            await send_temp(context, f"👋 欢迎管理员 {member.full_name}", config.GROUP_ID)
            continue

        await asyncio.sleep(1)

        # 如果数据库没有记录，添加试用
        row = get_user(user_id)
        from database import add_trial as db_add_trial
        if not row:
            db_add_trial(user_id)
            logging.info(f"新成员 {user_id} 入库并添加试用")

        # 有付费会员资格，清除试用期
        if row and row[0]:
            db_execute("UPDATE users SET trial_start_time=NULL, trial_reminded=0 WHERE user_id=?", (user_id,))

        # 优先检查封禁状态：被封禁用户无论试用/会员是否到期都立即踢出
        # （kick_user(ban=True) 会设置 is_banned=1 但不清除 trial_start_time，
        #  get_user_status 会因试用未到期优先返回 True，故需在此单独拦截）
        if row and row[3] == 1:
            logging.info(f"新成员 {user_id} 已被封禁（is_banned=1），立即踢出")
            await kick_user(context, user_id, "用户已被封禁", ban=True)
            continue

        # 立即检查用户资格
        from database import get_user_status
        is_valid, status = get_user_status(user_id)

        if not is_valid:
            # 无有效资格（试用到期/会员到期），立即踢出
            logging.info(f"新成员 {user_id} 无有效资格（{status}），立即踢出")
            await kick_user(context, user_id, status, ban=True)
            continue

        await send_temp(context, f"👋 欢迎 {member.full_name}\n{status}", config.GROUP_ID)


async def left_member_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理成员离开消息 - 自动删除"""
    msg = update.message
    if not msg or not msg.left_chat_member:
        return
    if msg.chat.id != config.GROUP_ID:
        return

    # ✅ 先删除系统消息
    try:
        await msg.delete()
    except Exception as e:
        logging.warning(f"删除离开消息失败: {e}")

    # ✅ 发送可自动删除的临时消息
    left_user = msg.left_chat_member
    await send_temp(
        context,
        f"⚠️ 用户 {left_user.full_name} 已离开群聊",
        config.GROUP_ID,
        delay=DELETE_DELAY
    )


async def auto_delete(message, delay: int):
    """自动删除消息的辅助函数"""
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except:
        pass
