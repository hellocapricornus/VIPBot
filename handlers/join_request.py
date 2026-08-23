# join_request.py - 完整修复版本
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import add_trial, get_user, get_user_status, unban_user, db_execute
import config

async def handle_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """用户申请加入群组时处理（基于数据库状态）"""
    request = update.chat_join_request
    user_id = request.from_user.id
    chat_id = request.chat.id

    logging.info(f"收到入群申请: 用户 {user_id}")

    if chat_id != config.GROUP_ID:
        await request.decline()
        return

    # 1. 检查频道关注
    from database import is_user_following_channel
    is_following = await is_user_following_channel(context, user_id)

    # API 异常时暂不处理，保留申请（避免误拒合法用户）
    if is_following is None:
        logging.warning(f"用户 {user_id} 频道关注检查失败（API异常），暂不处理入群申请")
        return

    if not is_following:
        await request.decline()
        logging.info(f"用户 {user_id} 未关注频道，拒绝入群")
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 关注频道", url=config.CHANNEL_LINK)],
            [InlineKeyboardButton("✅ 我已关注", callback_data="check_follow")]
        ])
        try:
            await context.bot.send_message(
                user_id,
                "❌ 您需要先关注频道才能加入群组。\n\n"
                f"👉 {config.CHANNEL_LINK}",
                reply_markup=keyboard
            )
        except:
            pass
        return

    # 2. 获取用户数据库状态
    row = get_user(user_id)
    is_valid, status = get_user_status(user_id)

    logging.info(f"用户 {user_id} 状态: is_valid={is_valid}, status={status}")

    # 3. 有会员资格，清除试用期并批准
    if row and row[0]:  # 有 expire_time（付费会员）
        db_execute("UPDATE users SET trial_start_time=NULL, trial_reminded=0, is_banned=0 WHERE user_id=?", (user_id,))
        await request.approve()
        logging.info(f"付费用户 {user_id} 批准入群")
        try:
            await context.bot.send_message(user_id, f"✅ 欢迎回归！\n\n{status}")
        except:
            pass
        return

    # 4. 数据库标记为封禁 - 拒绝
    if row and row[3] == 1:
        await request.decline()
        logging.info(f"用户 {user_id} 被封禁，拒绝入群")

        # 🔧 根据是否有付费记录显示不同提示
        has_paid = db_execute(
            "SELECT 1 FROM usdt_orders WHERE user_id=? AND status='paid' LIMIT 1",
            (user_id,)
        ).fetchone()

        if has_paid:
            tip = "您的会员已到期，请续费后重新申请。"
        elif row and row[2]:
            tip = "您的试用已到期，请购买会员后重新申请。"
        else:
            tip = "您暂无访问权限，请购买会员后重新申请。"

        try:
            await context.bot.send_message(
                user_id,
                f"❌ 无法加入群组\n\n{tip}\n\n"
                f"发送 /start 查看购买选项。"
            )
        except:
            pass
        return

    # 5. 有有效资格 - 批准
    if is_valid:
        await request.approve()
        logging.info(f"用户 {user_id} 有有效资格，批准入群")
        try:
            await context.bot.send_message(user_id, f"✅ 欢迎加入群组！\n\n{status}")
        except:
            pass
        return

    # 6. 无记录的新用户 - 添加试用并批准
    if not row:
        add_trial(user_id)
        await request.approve()
        logging.info(f"新用户 {user_id}，获得试用资格")
        try:
            await context.bot.send_message(
                user_id,
                f"✅ 欢迎加入群组！\n\n🧪 您已获得 {config.TRIAL_HOURS} 小时免费试用。\n试用结束后请购买会员。"
            )
        except:
            pass
        return

    # 7. 其他情况 - 拒绝
    await request.decline()
    logging.info(f"用户 {user_id} 无有效资格，拒绝入群")
    try:
        await context.bot.send_message(
            user_id,
            f"❌ 无法加入群组\n\n{status}\n\n发送 /start 查看购买选项。"
        )
    except:
        pass
