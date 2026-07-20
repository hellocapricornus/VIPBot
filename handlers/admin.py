# admin.py - 移除删除会员功能后的版本

import asyncio
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
import config
from config import ADMIN_ID
from database import is_admin, add_trial, add_permanent, extend_member, ban_user, unban_user, get_user, db_execute, now, save_message, remove_permanent, delete_user_membership, log_admin_action, BEIJING
from utils import kick_user, is_user_following_channel
from datetime import datetime, timedelta

# ================= 添加输入验证的辅助函数 =================
def parse_user_id(args, arg_index=0) -> tuple:
    """安全解析用户ID，返回 (success, user_id, error_message)"""
    if len(args) <= arg_index:
        return False, None, "缺少用户ID参数"
    try:
        user_id = int(args[arg_index])
        return True, user_id, None
    except ValueError:
        return False, None, f"用户ID必须是数字，收到: {args[arg_index]}"

def parse_extend_args(args) -> tuple:
    """安全解析 extend 命令参数"""
    if len(args) < 2:
        return False, None, None, "用法: /extend 用户ID 天数"
    try:
        user_id = int(args[0])
        days = int(args[1])
        if days <= 0:
            return False, None, None, "天数必须大于0"
        return True, user_id, days, None
    except ValueError as e:
        return False, None, None, f"参数格式错误: {e}"

# ================= 命令函数 =================
async def cmd_add_trial(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config.refresh_config()
    if not is_admin(update.effective_user.id):
        return

    success, uid, error = parse_user_id(context.args)
    if not success:
        await update.message.reply_text(f"❌ {error}\n用法: /add_trial 用户ID")
        return

    add_trial(uid)

    log_admin_action(update.effective_user.id, "add_trial", uid)
    await update.message.reply_text(f"✅ 已为用户 {uid} 添加{config.TRIAL_HOURS}小时试用")

async def cmd_add_permanent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config.refresh_config()
    if not is_admin(update.effective_user.id):
        return

    success, uid, error = parse_user_id(context.args)
    if not success:
        await update.message.reply_text(f"❌ {error}\n用法: /add_permanent 用户ID")
        return

    add_permanent(uid)

    log_admin_action(update.effective_user.id, "add_permanent", uid)
    await update.message.reply_text(f"✅ 已将用户 {uid} 设为永久会员")

async def cmd_extend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config.refresh_config()
    if not is_admin(update.effective_user.id):
        return
    success, uid, days, error = parse_extend_args(context.args)
    if not success:
        await update.message.reply_text(f"❌ {error}")
        return

    logging.info(f"=== 执行 extend 命令 ===, 用户ID: {uid}, 天数: {days}")

    before = db_execute("SELECT expire_time, trial_start_time, is_permanent, is_banned FROM users WHERE user_id=?", (uid,)).fetchone()
    logging.info(f"执行前: expire={before[0] if before else None}, trial={before[2] if before else None}")

    new_expire = extend_member(uid, days)

    after = db_execute("SELECT expire_time, trial_start_time, is_permanent, is_banned FROM users WHERE user_id=?", (uid,)).fetchone()
    logging.info(f"执行后: expire={after[0] if after else None}, trial={after[2] if after else None}")

    from database import get_user_status
    is_valid, status = get_user_status(uid)
    logging.info(f"执行后用户状态: is_valid={is_valid}, status={status}")

    log_admin_action(update.effective_user.id, "extend", uid)

    await update.message.reply_text(
        f"✅ 已为用户 {uid} 延长 {days} 天\n"
        f"新到期时间: {new_expire.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"当前状态: {status}"
    )

async def cmd_kick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config.refresh_config()
    if not is_admin(update.effective_user.id):
        return
    success, uid, error = parse_user_id(context.args)
    if not success:
        await update.message.reply_text(f"❌ {error}\n用法: /kick 用户ID [原因]")
        return

    reason = " ".join(context.args[1:]) if len(context.args) > 1 else "管理员操作"

    log_admin_action(update.effective_user.id, "kick", uid)
    await kick_user(context, uid, reason)
    await update.message.reply_text(f"已踢出并封禁用户 {uid}")

async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config.refresh_config()
    if not is_admin(update.effective_user.id):
        return
    success, uid, error = parse_user_id(context.args)
    if not success:
        await update.message.reply_text(f"❌ {error}\n用法: /unban 用户ID")
        return
    unban_user(uid)

    log_admin_action(update.effective_user.id, "unban", uid)
    await update.message.reply_text(f"已解封用户 {uid}")

# ❌ 删除 cmd_delete_member 函数（已移除）

# admin.py - 添加调试命令

async def cmd_check_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """调试命令：查看用户状态（不会被定时任务删除）"""
    config.refresh_config()
    if not is_admin(update.effective_user.id):
        return

    success, uid, error = parse_user_id(context.args)
    if not success:
        await update.message.reply_text(f"❌ {error}\n用法: /check_user 用户ID")
        return

    from database import get_user_status, db_execute

    # 获取数据库原始数据
    row = db_execute("SELECT user_id, expire_time, is_permanent, trial_start_time, is_banned FROM users WHERE user_id=?", (uid,)).fetchone()

    if not row:
        await update.message.reply_text(f"❌ 用户 {uid} 不存在于数据库中")
        return

    is_valid, status = get_user_status(uid)

    # 检查用户在群组中的状态
    try:
        member = await context.bot.get_chat_member(config.GROUP_ID, uid)
        group_status = member.status
    except Exception as e:
        group_status = f"检查失败: {e}"

    text = f"📊 **用户 {uid} 状态**\n\n"
    text += f"存在于数据库: ✅\n"
    text += f"永久会员: {'是' if row[2] else '否'}\n"
    text += f"到期时间: {row[1] or '无'}\n"
    text += f"试用开始: {row[3] or '无'}\n"
    text += f"是否封禁: {'是' if row[4] else '否'}\n"
    text += f"有效状态: {is_valid}\n"
    text += f"状态描述: {status}\n"
    text += f"群组状态: {group_status}\n"

    await update.message.reply_text(text, parse_mode="Markdown")

# ================= 管理员回调 =================
async def back_to_admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config.refresh_config()
    query = update.callback_query
    await query.answer()
    context.user_data.pop('replying_to_user', None)
    context.user_data.pop('last_settings_click', None)

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 数据统计", callback_data="admin_stats")],
        [InlineKeyboardButton("👥 用户管理", callback_data="admin_user_manage"),
         InlineKeyboardButton("💳 会员管理", callback_data="admin_member_manage")],
        [InlineKeyboardButton("💎 USDT订单", callback_data="admin_usdt_orders"),
         InlineKeyboardButton("📜 订单历史", callback_data="admin_usdt_orders_history")],
        [InlineKeyboardButton("📦 套餐管理", callback_data="admin_plans"),
         InlineKeyboardButton("🏦 地址管理", callback_data="admin_addresses")],
        [InlineKeyboardButton("📢 广播消息", callback_data="admin_broadcast"),
         InlineKeyboardButton("💬 回复用户", callback_data="admin_reply")],
        [InlineKeyboardButton("⚙️ 系统设置", callback_data="admin_settings")],
    ])
    await query.edit_message_text("👑 管理员菜单", reply_markup=keyboard)

# ================= 用户管理子菜单 =================
async def admin_user_manage_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config.refresh_config()
    """用户管理子菜单"""
    query = update.callback_query
    await query.answer()

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ 添加临时", callback_data="admin_add_trial"),
         InlineKeyboardButton("⭐ 添加永久", callback_data="admin_add_permanent")],
        [InlineKeyboardButton("👢 踢出用户", callback_data="admin_kick"),
         InlineKeyboardButton("🔓 解封用户", callback_data="admin_unban")],
        [InlineKeyboardButton("◀️ 返回", callback_data="back_to_admin_menu")]
    ])
    await query.edit_message_text("👥 用户管理\n\n选择操作：", reply_markup=keyboard)
# ================= 会员管理子菜单 =================
async def admin_member_manage_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config.refresh_config()
    """会员管理子菜单"""
    query = update.callback_query
    await query.answer()

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 会员列表", callback_data="admin_members"),
         InlineKeyboardButton("🧪 试用列表", callback_data="admin_trials")],
        [InlineKeyboardButton("🚫 封禁列表", callback_data="admin_banned")],
        [InlineKeyboardButton("⏰ 延长会员", callback_data="admin_extend")],
        [InlineKeyboardButton("◀️ 返回", callback_data="back_to_admin_menu")]
    ])
    await query.edit_message_text("💳 会员管理\n\n选择操作：", reply_markup=keyboard)
# ================= 套餐管理回调 =================
async def admin_plans_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config.refresh_config()
    query = update.callback_query
    await query.answer()

    from database import get_all_plans
    plans = get_all_plans()

    if not plans:
        await query.edit_message_text(
            "📭 **暂无套餐**\n\n"
            "💡 **添加套餐**\n"
            "`/addplan 套餐ID 名称 天数 价格`\n"
            "例如：`/addplan buy_1m 月度会员 30 40`\n\n"
            "💡 **删除套餐**\n"
            "`/delplan 套餐ID`\n"
            "例如：`/delplan buy_1m`\n\n"
            "💡 **启/禁套餐**\n"
            "`/toggleplan 套餐ID`\n"
            "例如：`/toggleplan buy_1m`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ 返回", callback_data="back_to_admin_menu")]
            ])
        )
        return

    text = "📦 **套餐列表**\n\n"
    for p in plans:
        status = "✅ 启用" if p['is_active'] else "❌ 禁用"
        text += f"{status} | `{p['plan_id']}`\n"
        text += f"  📛 {p['name']}\n"
        text += f"  💰 {p['price']} USDT / {p['days']}天\n\n"

    text += (
        "💡 **管理命令**\n"
        "`/addplan 套餐ID 名称 天数 价格` - 添加\n"
        "`/delplan 套餐ID` - 删除\n"
        "`/toggleplan 套餐ID` - 启用/禁用"
    )

    await query.edit_message_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("◀️ 返回", callback_data="back_to_admin_menu")]
        ])
    )

# ================= 地址管理回调 =================
async def admin_addresses_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config.refresh_config()
    query = update.callback_query
    await query.answer()

    from database import db_execute
    rows = db_execute("SELECT address, status FROM vip_addresses ORDER BY added_at").fetchall()

    if not rows:
        await query.edit_message_text(
            "📭 **暂无收款地址**\n\n"
            "💡 **添加地址**\n"
            "`/addaddr TRC20地址`\n"
            "例如：`/addaddr TWYctLLCbvavefuCqRXxgKzS7hVe6cpbp9`\n\n"
            "💡 **删除地址**\n"
            "`/deladdr TRC20地址`\n"
            "例如：`/deladdr TWYctLLCbvavefuCqRXxgKzS7hVe6cpbp9`\n\n"
            "⚠️ 只能添加 TRC20 网络地址（T开头，34位）",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ 返回", callback_data="back_to_admin_menu")]
            ])
        )
        return

    idle_count = sum(1 for _, s in rows if s == 'idle')
    used_count = len(rows) - idle_count

    text = f"🏦 **收款地址池**\n\n🟢 空闲：{idle_count} | 🔴 使用中：{used_count}\n\n"
    for addr, status in rows:
        icon = "🟢" if status == "idle" else "🔴"
        text += f"{icon} `{addr}`\n"

    text += (
        "\n💡 **管理命令**\n"
        "`/addaddr TRC20地址` - 添加\n"
        "`/deladdr TRC20地址` - 删除"
    )

    await query.edit_message_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("◀️ 返回", callback_data="back_to_admin_menu")]
        ])
    )

async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config.refresh_config()
    query = update.callback_query
    await query.answer()
    from database import db_execute
    from datetime import datetime

    total_users = db_execute("SELECT COUNT(*) FROM users").fetchone()[0]
    trial_users = db_execute("SELECT COUNT(*) FROM users WHERE trial_start_time IS NOT NULL AND expire_time IS NULL AND is_permanent=0 AND is_banned=0").fetchone()[0]
    paid_users = db_execute("SELECT COUNT(*) FROM users WHERE expire_time IS NOT NULL AND is_permanent=0 AND is_banned=0").fetchone()[0]
    permanent = db_execute("SELECT COUNT(*) FROM users WHERE is_permanent=1 AND is_banned=0").fetchone()[0]
    banned = db_execute("SELECT COUNT(*) FROM banned").fetchone()[0]

    text = f"📊 **数据统计**\n\n"
    text += f"总用户：{total_users}\n"
    text += f"试用中：{trial_users}\n"
    text += f"付费会员：{paid_users}\n"
    text += f"永久会员：{permanent}\n"
    text += f"封禁：{banned}\n\n"

    # ✅ 显示付费用户详情
    members = db_execute("""
        SELECT user_id, expire_time, is_permanent 
        FROM users 
        WHERE (expire_time IS NOT NULL OR is_permanent=1) AND is_banned=0
        ORDER BY expire_time ASC
    """).fetchall()

    if members:
        text += "━━━━━━━━━━━━\n📋 **付费用户详情**\n\n"
        for uid, exp, perm in members:
            try:
                user = await context.bot.get_chat(uid)
                name = user.full_name or "未知"
                username = f" @{user.username}" if user.username else ""
                display = f"{name}{username}"
            except:
                display = str(uid)

            if perm:
                text += f"• {display}\n  🆔 `{uid}` | 永久会员\n\n"
            elif exp:
                exp_date = datetime.fromisoformat(exp).astimezone(BEIJING).strftime("%Y-%m-%d %H:%M")
                text += f"• {display}\n  🆔 `{uid}` | 到期 {exp_date}\n\n"

    # ✅ 显示试用用户详情
    trials = db_execute("""
        SELECT user_id, trial_start_time 
        FROM users 
        WHERE trial_start_time IS NOT NULL AND expire_time IS NULL AND is_permanent=0 AND is_banned=0
        ORDER BY trial_start_time ASC
    """).fetchall()

    if trials:
        text += "━━━━━━━━━━━━\n🧪 **试用用户详情**\n\n"
        for uid, start in trials:
            try:
                user = await context.bot.get_chat(uid)
                name = user.full_name or "未知"
                username = f" @{user.username}" if user.username else ""
                display = f"{name}{username}"
            except:
                display = str(uid)

            start_time = datetime.fromisoformat(start).astimezone(BEIJING)
            end_time = start_time + timedelta(hours=config.TRIAL_HOURS)
            text += f"• {display}\n  🆔 `{uid}` | 到期 {end_time.strftime('%Y-%m-%d %H:%M')}\n\n"

    await query.edit_message_text(
        text, 
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("◀️ 返回", callback_data="back_to_admin_menu")]
        ])
    )

async def admin_add_trial(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config.refresh_config()
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("请回复: /add_trial 用户ID\n例如: /add_trial 123456789", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ 返回", callback_data="back_to_admin_menu")]]))

async def admin_add_permanent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config.refresh_config()
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("请回复: /add_permanent 用户ID", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ 返回", callback_data="back_to_admin_menu")]]))

async def admin_extend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config.refresh_config()
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("请回复: /extend 用户ID 天数", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ 返回", callback_data="back_to_admin_menu")]]))

async def admin_kick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config.refresh_config()
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("请回复: /kick 用户ID [原因]", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ 返回", callback_data="back_to_admin_menu")]]))

async def admin_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config.refresh_config()
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("请回复: /unban 用户ID", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ 返回", callback_data="back_to_admin_menu")]]))

# 新增：删除会员回调
async def admin_delete_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config.refresh_config()
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "⚠️ **删除会员功能**\n\n"
        "此操作将：\n"
        "1. 删除用户的所有会员资格（包括永久会员）\n"
        "2. 将用户封禁\n"
        "3. 踢出群组\n\n"
        "请回复: /delete_member 用户ID\n"
        "例如: /delete_member 123456789\n\n"
        "⚠️ 此操作不可撤销！",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ 返回", callback_data="back_to_admin_menu")]]),
        parse_mode="Markdown"
    )

async def admin_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config.refresh_config()
    query = update.callback_query
    await query.answer()
    from database import db_execute
    from datetime import datetime
    rows = db_execute("SELECT user_id, expire_time, is_permanent FROM users WHERE (expire_time IS NOT NULL OR is_permanent=1) AND is_banned=0").fetchall()
    if not rows:
        await query.edit_message_text("暂无会员", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ 返回", callback_data="back_to_admin_menu")]]))
        return
    text = "📋 会员列表\n"
    for uid, exp, perm in rows[:20]:
        try:
            user = await context.bot.get_chat(uid)
            name = user.full_name or user.username or str(uid)
        except:
            name = str(uid)
        if perm:
            text += f"• {name} ({uid}) - 永久会员\n"
        else:
            exp_date = datetime.fromisoformat(exp).astimezone(BEIJING).strftime("%Y-%m-%d")
            text += f"• {name} ({uid}) - 到期 {exp_date}\n"
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ 返回", callback_data="back_to_admin_menu")]]))

async def admin_trials(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config.refresh_config()
    query = update.callback_query
    await query.answer()
    from database import db_execute
    from datetime import datetime, timedelta
    rows = db_execute("SELECT user_id, trial_start_time FROM users WHERE trial_start_time IS NOT NULL AND expire_time IS NULL AND is_permanent=0 AND is_banned=0").fetchall()
    if not rows:
        await query.edit_message_text("暂无试用用户", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ 返回", callback_data="back_to_admin_menu")]]))
        return
    text = "🧪 试用用户\n"
    for uid, start in rows[:20]:
        start_time = datetime.fromisoformat(start).astimezone(BEIJING)
        end_time = start_time + timedelta(hours=config.TRIAL_HOURS)
        try:
            user = await context.bot.get_chat(uid)
            name = user.full_name or user.username or str(uid)
        except:
            name = str(uid)
        text += f"• {name} ({uid}) - 到期 {end_time.strftime('%Y-%m-%d %H:%M')}\n"
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ 返回", callback_data="back_to_admin_menu")]]))

async def admin_banned(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config.refresh_config()
    query = update.callback_query
    await query.answer()
    from database import db_execute
    rows = db_execute("SELECT user_id, reason FROM banned").fetchall()
    if not rows:
        await query.edit_message_text("暂无封禁用户", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ 返回", callback_data="back_to_admin_menu")]]))
        return
    text = "🚫 封禁列表\n"
    for uid, reason in rows[:20]:
        text += f"• {uid} - {reason}\n"
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ 返回", callback_data="back_to_admin_menu")]]))

# ================= 管理员回复用户 =================
async def admin_reply_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config.refresh_config()
    """管理员回复用户 - 显示最近联系的用户列表"""
    query = update.callback_query
    await query.answer()

    from database import db_execute

    rows = db_execute("""
        SELECT DISTINCT from_user, MAX(timestamp) 
        FROM messages 
        WHERE to_user=? 
        GROUP BY from_user 
        ORDER BY MAX(timestamp) DESC 
        LIMIT 20
    """, (ADMIN_ID,)).fetchall()

    if not rows:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("◀️ 返回", callback_data="back_to_admin_menu")]
        ])
        await query.edit_message_text(
            "📭 暂无用户联系记录。\n\n当用户联系管理员时，会显示在这里。",
            reply_markup=keyboard
        )
        return

    buttons = []
    for row in rows:
        user_id = row[0]
        try:
            member = await context.bot.get_chat_member(config.GROUP_ID, user_id)
            name = member.user.full_name
        except:
            try:
                user = await context.bot.get_chat(user_id)
                name = user.full_name or user.username or str(user_id)
            except:
                name = str(user_id)

        buttons.append([InlineKeyboardButton(f"💬 {name}", callback_data=f"reply_user_{user_id}")])

    buttons.append([InlineKeyboardButton("◀️ 返回", callback_data="back_to_admin_menu")])

    await query.edit_message_text(
        "📋 最近联系的用户：\n\n点击用户即可回复。",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def admin_reply_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config.refresh_config()
    """管理员回复用户消息 - 使用命令 /reply"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ 只有管理员可以使用此命令。")
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "用法：/reply 用户ID 回复内容\n\n"
            "例如：/reply 123456 你好，有什么可以帮助你的？\n\n"
            "或者点击用户消息下方的「回复用户」按钮。"
        )
        return

    try:
        user_id = int(context.args[0])
        reply_text = " ".join(context.args[1:])

        save_message(ADMIN_ID, user_id, reply_text)

        await context.bot.send_message(
            user_id,
            f"📨 管理员回复：\n\n{reply_text}\n\n"
            f"如需继续联系，请使用 /start 后点击「联系管理员」。"
        )
        await update.message.reply_text(f"✅ 已回复用户 {user_id}")
    except ValueError:
        await update.message.reply_text("❌ 用户ID必须是数字。")
    except Exception as e:
        await update.message.reply_text(f"❌ 发送失败：{e}")

# ================= 广播消息功能 =================
async def admin_broadcast_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config.refresh_config()
    """广播消息回调 - 进入广播模式"""
    query = update.callback_query
    await query.answer()

    context.user_data['broadcast_mode'] = True
    context.user_data['broadcast_pending'] = None  # 待确认的消息

    await query.edit_message_text(
        "📢 **广播消息**\n\n"
        "请发送要广播的内容（文字/图片/视频/GIF/文件等）\n\n"
        "发送后会先预览，确认后再发送给所有用户。\n\n"
        "回复 /cancel 取消广播",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("◀️ 取消", callback_data="back_to_admin_menu")]
        ])
    )
async def handle_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config.refresh_config()
    """处理广播消息 - 先预览再确认"""
    if not is_admin(update.effective_user.id):
        return

    if not context.user_data.get('broadcast_mode'):
        return

    msg = update.message

    # 取消
    if msg.text and msg.text == "/cancel":
        context.user_data['broadcast_mode'] = False
        context.user_data['broadcast_pending'] = None
        await update.message.reply_text("✅ 已取消广播")
        return

    # ✅ 第一步：存储消息，发送预览
    if not context.user_data.get('broadcast_pending'):
        # 先回复确认
        await msg.reply_text(
            "📋 **预览**\n\n"
            "👆 这是你要广播的内容\n\n"
            "确认发送给所有用户吗？",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ 确认发送", callback_data="broadcast_confirm")],
                [InlineKeyboardButton("❌ 取消", callback_data="broadcast_cancel")],
            ])
        )
        # 存储消息引用
        context.user_data['broadcast_pending'] = {
            'chat_id': msg.chat_id,
            'message_id': msg.message_id
        }
        return

    # 如果已经有待确认的消息，提醒先处理
    await msg.reply_text("⚠️ 请先处理上一条广播（确认或取消）")
async def broadcast_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config.refresh_config()
    """确认发送广播"""
    query = update.callback_query
    await query.answer()

    pending = context.user_data.get('broadcast_pending')
    if not pending:
        await query.edit_message_text("❌ 没有待发送的广播")
        return

    await query.edit_message_text("⏳ 正在发送广播...")

    # ✅ 改用 copy_message 获取原始消息
    try:
        message_id = await context.bot.copy_message(
            chat_id=query.message.chat_id,
            from_chat_id=pending['chat_id'],
            message_id=pending['message_id']
        )
    except:
        await query.edit_message_text("❌ 无法获取原始消息，请重新发送")
        context.user_data['broadcast_pending'] = None
        return

    from database import get_all_users_for_broadcast
    users = get_all_users_for_broadcast()

    success_count = 0
    fail_count = 0

    for (uid,) in users:
        try:
            # ✅ 直接 copy 消息给用户，保留所有格式
            await context.bot.copy_message(
                chat_id=uid,
                from_chat_id=pending['chat_id'],
                message_id=pending['message_id']
            )
            success_count += 1
            await asyncio.sleep(0.05)

        except Exception as e:
            fail_count += 1
            logging.warning(f"广播给 {uid} 失败: {e}")

    context.user_data['broadcast_mode'] = False
    context.user_data['broadcast_pending'] = None
    log_admin_action(query.from_user.id, f"broadcast: 成功{success_count} 失败{fail_count}")

    await query.edit_message_text(f"✅ 广播完成！\n\n成功: {success_count}\n失败: {fail_count}")
async def broadcast_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config.refresh_config()
    """取消广播"""
    query = update.callback_query
    await query.answer()

    context.user_data['broadcast_pending'] = None

    await query.edit_message_text(
        "❌ 已取消广播\n\n可以重新发送内容。",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("◀️ 返回菜单", callback_data="back_to_admin_menu")]
        ])
    )

# ================= 定时任务 =================
async def check_expired(context: ContextTypes.DEFAULT_TYPE):
    config.refresh_config()
    """检查试用和会员到期，以及频道关注状态"""
    from datetime import datetime, timedelta
    from database import db_execute, now, get_user_status
    from utils import kick_user
    from database import is_admin as check_admin
    import logging

    current = now()
    logging.info("定时任务执行：检查频道关注、试用到期、会员到期")

    # ================= 1. 检查频道关注状态 =================
    all_users = db_execute("SELECT user_id, expire_time, is_permanent FROM users WHERE is_banned=0").fetchall()

    for (uid, expire_time, is_permanent) in all_users:
        if check_admin(uid):
            continue

        is_valid, status = get_user_status(uid)

        try:
            member = await context.bot.get_chat_member(config.GROUP_ID, uid)
            if member.status not in ["member", "administrator", "creator"]:
                logging.info(f"用户 {uid} 不在群组中，保留记录 (状态: {status})")
                continue
        except Exception as e:
            logging.info(f"检查用户 {uid} 群组状态失败: {e}")
            continue

        from handlers.user import check_and_handle_channel
        await check_and_handle_channel(context, uid, kick_only=True)

    # ================= 2. 检查试用到期 =================
    trials = db_execute("SELECT user_id, trial_start_time, reminded_type FROM users WHERE trial_start_time IS NOT NULL AND expire_time IS NULL AND is_permanent=0 AND is_banned=0").fetchall()
    for uid, start, reminded_type in trials:
        try:
            start_time = datetime.fromisoformat(start).astimezone(BEIJING)
            end_time = start_time + timedelta(hours=config.TRIAL_HOURS)

            if current >= end_time:
                logging.info(f"用户 {uid} 试用到期，准备封禁并踢出")
                await kick_user(context, uid, "试用到期", ban=True)
                db_execute("UPDATE users SET is_banned=1, reminded_type=NULL WHERE user_id=?", (uid,))
            elif (end_time - current) <= timedelta(hours=config.REMIND_HOURS):
                if reminded_type != 'trial':
                    try:
                        await context.bot.send_message(
                            uid, 
                            f"⏰ **试用即将到期**\n\n您的试用剩余 {config.REMIND_HOURS} 小时，请购买会员以继续使用。\n\n发送 /start 查看购买选项。"
                        )
                        db_execute("UPDATE users SET reminded_type='trial' WHERE user_id=?", (uid,))
                    except:
                        pass
        except Exception as e:
            logging.error(f"处理试用用户 {uid} 时出错: {e}")

    # ================= 3. 检查会员到期 =================
    members = db_execute("SELECT user_id, expire_time, reminded_type FROM users WHERE expire_time IS NOT NULL AND is_permanent=0 AND is_banned=0").fetchall()
    for uid, exp, reminded_type in members:
        try:
            expire = datetime.fromisoformat(exp).astimezone(BEIJING)

            if current >= expire:
                logging.info(f"用户 {uid} 会员到期，准备封禁并踢出")
                await kick_user(context, uid, "会员到期", ban=True)
                db_execute("UPDATE users SET is_banned=1, reminded_type=NULL WHERE user_id=?", (uid,))
            elif (expire - current) <= timedelta(days=config.MEMBER_REMIND_DAYS):
                if reminded_type != 'member':
                    days_left = (expire - current).days
                    try:
                        await context.bot.send_message(
                            uid,
                            f"⏰ **会员即将到期**\n\n您的会员还剩 {days_left} 天到期，请及时续费。\n\n发送 /start 查看续费选项。"
                        )
                        db_execute("UPDATE users SET reminded_type='member' WHERE user_id=?", (uid,))
                    except:
                        pass
        except Exception as e:
            logging.error(f"处理会员用户 {uid} 时出错: {e}")

async def admin_usdt_orders_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config.refresh_config()
    """管理员查看待处理 USDT 订单 - 添加手动确认按钮"""
    query = update.callback_query
    await query.answer()

    from handlers.user import pending_usdt_orders, clean_expired_orders
    USDT_ORDER_TIMEOUT = config.USDT_ORDER_TIMEOUT
    import time

    clean_expired_orders()

    if not pending_usdt_orders:
        await query.edit_message_text(
            "📭 当前没有待处理的 USDT 订单。",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ 返回", callback_data="back_to_admin_menu")]
            ])
        )
        return

    text = "💎 **待处理 USDT 订单**\n\n"
    buttons = []

    for amount_key, order in pending_usdt_orders.items():
        remaining = int(USDT_ORDER_TIMEOUT - (time.time() - order["created_at"]))
        minutes = remaining // 60
        seconds = remaining % 60
        text += f"• 用户ID: `{order['user_id']}`\n"
        text += f"  套餐: {order['plan_name']}\n"
        text += f"  金额: {order['amount']} USDT\n"
        text += f"  剩余: {minutes}分{seconds}秒\n"
        text += f"  订单号: `{order['order_id']}`\n\n"

        # 添加手动确认按钮
        buttons.append([InlineKeyboardButton(
            f"✅ 手动确认 - {order['plan_name']} ({order['amount']} USDT)", 
            callback_data=f"admin_confirm_usdt_{amount_key}"
        )])

    buttons.append([InlineKeyboardButton("🔄 刷新", callback_data="admin_usdt_orders")])
    buttons.append([InlineKeyboardButton("◀️ 返回", callback_data="back_to_admin_menu")])

    await query.edit_message_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def admin_confirm_usdt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config.refresh_config()
    """管理员手动确认 USDT 订单 - 添加频道检查"""
    query = update.callback_query
    await query.answer()

    amount_key = query.data.replace("admin_confirm_usdt_", "")

    from handlers.user import pending_usdt_orders
    from database import extend_member, unban_user, db_execute, now, get_user_status

    if amount_key not in pending_usdt_orders:
        await query.edit_message_text("❌ 订单不存在或已过期")
        return

    order = pending_usdt_orders[amount_key]
    user_id = order["user_id"]
    days = order["days"]
    plan_name = order["plan_name"]

    # 添加频道检查
    from database import is_user_following_channel
    is_following = await is_user_following_channel(context, user_id)
    if is_following is None:
        logging.warning(f"用户 {user_id} 频道关注检查失败（API异常），跳过频道检查继续处理")
    elif not is_following:
        await query.edit_message_text(
            f"⚠️ 用户 {user_id} 未关注频道！\n\n"
            f"请先让用户关注频道后再确认订单。\n\n"
            f"👉 {config.CHANNEL_LINK}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ 返回订单列表", callback_data="admin_usdt_orders")]
            ])
        )
        return

    logging.info(f"=== 手动确认订单 ===, 用户ID: {user_id}, 天数: {days}, 套餐: {plan_name}")

    # 1. 解封用户（数据库）
    unban_user(user_id)

    # 2. 延长会员时间
    new_expire = extend_member(user_id, days)
    logging.info(f"会员延期后到期时间: {new_expire}")

    # 3. 获取用户状态
    is_valid, status = get_user_status(user_id)

    # 5. 更新数据库中的订单状态
    db_execute("""
        UPDATE usdt_orders 
        SET status='paid', paid_at=?, tx_id='manual_confirm'
        WHERE order_id=?
    """, (now().isoformat(), order["order_id"]))

    # ✅ 释放地址
    from database import mark_address_idle
    if "address" in order:
        mark_address_idle(order["address"])

    # 6. 删除内存中的订单
    del pending_usdt_orders[amount_key]

    # 7. 通知用户
    try:
        await context.bot.send_message(
            user_id,
            f"✅ **支付已确认！**\n\n"
            f"套餐：{plan_name}\n"
            f"会员到期时间：{new_expire.strftime('%Y-%m-%d %H:%M')}\n\n"
            f"感谢您的支持！"
        )
    except Exception as e:
        logging.warning(f"通知用户失败: {e}")

    log_admin_action(update.effective_user.id, f"manual_confirm_usdt_{order['order_id']}", user_id)

    await query.edit_message_text(
        f"✅ 已手动确认订单\n\n"
        f"用户ID: {user_id}\n"
        f"套餐: {plan_name}\n"
        f"天数: {days}\n"
        f"已开通会员至: {new_expire.strftime('%Y-%m-%d %H:%M')}\n"
        f"用户状态: {status}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("◀️ 返回订单列表", callback_data="admin_usdt_orders")]
        ])
    )

async def admin_usdt_orders_history_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config.refresh_config()
    """管理员查看 USDT 订单历史 - 支持分类筛选和分页"""
    query = update.callback_query
    await query.answer()

    # 解析回调数据：admin_usdt_orders_history_{filter}_{page}
    data_parts = query.data.split("_")

    # 默认值
    order_filter = "all"
    page = 1

    # admin_usdt_orders_history → all, page 1
    # admin_usdt_orders_history_paid_2 → paid, page 2
    if len(data_parts) > 4:
        order_filter = data_parts[4]
    if len(data_parts) > 5:
        try:
            page = int(data_parts[5])
        except:
            page = 1

    per_page = 5  # 每页显示条数

    from database import db_execute
    from datetime import datetime

    # 构建查询条件
    filter_map = {
        "all": "1=1",
        "pending": "status='pending'",
        "paid": "status='paid'",
        "expired": "status IN ('expired', 'cancelled')"
    }
    where_clause = filter_map.get(order_filter, "1=1")

    # 查询总数
    count_row = db_execute(f"SELECT COUNT(*) FROM usdt_orders WHERE {where_clause}").fetchone()
    total = count_row[0] if count_row else 0
    total_pages = max(1, (total + per_page - 1) // per_page)

    if page > total_pages:
        page = total_pages

    offset = (page - 1) * per_page

    # 查询订单
    rows = db_execute(f"""
        SELECT order_id, user_id, plan_name, amount, status, created_at, paid_at, tx_id
        FROM usdt_orders 
        WHERE {where_clause}
        ORDER BY created_at DESC 
        LIMIT ? OFFSET ?
    """, (per_page, offset)).fetchall()

    if not rows:
        # 空状态
        filter_names = {"all": "订单", "pending": "待支付订单", "paid": "已支付订单", "expired": "已过期/取消订单"}
        await query.edit_message_text(
            f"📭 暂无{filter_names.get(order_filter, '订单')}记录。",
            reply_markup=_build_history_keyboard(order_filter, page, total_pages)
        )
        return

    # 统计各状态数量
    stats = db_execute("""
        SELECT 
            COUNT(*) as total,
            SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) as pending,
            SUM(CASE WHEN status='paid' THEN 1 ELSE 0 END) as paid,
            SUM(CASE WHEN status IN ('expired','cancelled') THEN 1 ELSE 0 END) as expired
        FROM usdt_orders
    """).fetchone()

    text = "💎 **USDT 订单历史**\n\n"
    text += f"📊 全部: {stats[0]} | ⏳待支付: {stats[1]} | ✅已支付: {stats[2]} | ❌已过期: {stats[3]}\n"
    text += f"━━━━━━━━━━━━\n"

    # 构建订单列表
    for row_data in rows:
        order_id, user_id, plan_name, amount, status, created_at, paid_at, tx_id = row_data

        # 获取用户信息
        try:
            user = await context.bot.get_chat(user_id)
            if user.username:
                user_display = f"{user.full_name} (@{user.username})"
            else:
                user_display = f"{user.full_name}"
            user_display += f"\n  🆔 `{user_id}`"
        except:
            user_display = f"🆔 `{user_id}`"

        status_map = {
            "pending": ("⏳", "待支付"),
            "paid": ("✅", "已支付"),
            "expired": ("❌", "已过期"),
            "cancelled": ("🚫", "已取消")
        }
        icon, status_text = status_map.get(status, ("❓", status))

        created_time = datetime.fromisoformat(created_at).astimezone(BEIJING).strftime("%m-%d %H:%M")

        text += f"{icon} **{plan_name}** - {amount} USDT\n"
        text += f"  👤 {user_display}\n"
        text += f"  📅 创建: {created_time}\n"

        if status == "paid" and paid_at:
            paid_time = datetime.fromisoformat(paid_at).astimezone(BEIJING).strftime("%m-%d %H:%M")
            text += f"  💰 支付: {paid_time}\n"

        if tx_id and tx_id != 'manual_confirm':
            text += f"  🔗 交易: `{tx_id[:20]}...`\n"
        elif tx_id == 'manual_confirm':
            text += f"  🔗 确认: 手动确认\n"

        text += "\n"

    text += f"━━━━━━━━━━━━\n📄 第 {page}/{total_pages} 页"

    await query.edit_message_text(
        text,
        parse_mode="Markdown",
        reply_markup=_build_history_keyboard(order_filter, page, total_pages)
    )
def _build_history_keyboard(current_filter: str, page: int, total_pages: int) -> InlineKeyboardMarkup:
    """构建订单历史页面的键盘"""
    buttons = []

    # 筛选按钮
    filter_buttons = [
        ("📋 全部", "all"),
        ("⏳ 待支付", "pending"),
        ("✅ 已支付", "paid"),
        ("❌ 已过期", "expired"),
    ]

    row = []
    for label, f in filter_buttons:
        if f == current_filter:
            row.append(InlineKeyboardButton(f"● {label}", callback_data=f"admin_usdt_orders_history_{f}_1"))
        else:
            row.append(InlineKeyboardButton(label, callback_data=f"admin_usdt_orders_history_{f}_1"))
    buttons.append(row)

    # 翻页按钮
    nav_row = []
    if page > 1:
        nav_row.append(InlineKeyboardButton("◀️ 上一页", callback_data=f"admin_usdt_orders_history_{current_filter}_{page-1}"))
    if page < total_pages:
        nav_row.append(InlineKeyboardButton("下一页 ▶️", callback_data=f"admin_usdt_orders_history_{current_filter}_{page+1}"))
    if nav_row:
        buttons.append(nav_row)

    # 底部按钮
    buttons.append([InlineKeyboardButton("📊 待处理订单", callback_data="admin_usdt_orders")])
    buttons.append([InlineKeyboardButton("◀️ 返回", callback_data="back_to_admin_menu")])

    return InlineKeyboardMarkup(buttons)

async def cmd_add_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config.refresh_config()
    if not is_admin(update.effective_user.id):
        return
    args = context.args
    if len(args) < 4:
        await update.message.reply_text("用法: /addplan 套餐ID 名称 天数 价格\n例如: /addplan buy_1m 月度会员 30 40")
        return
    plan_id, name, days, price = args[0], args[1], int(args[2]), float(args[3])
    from database import add_plan
    add_plan(plan_id, name, days, price)
    await update.message.reply_text(f"✅ 已添加套餐: {name} - {price} USDT / {days}天")

async def cmd_del_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config.refresh_config()
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("用法: /delplan 套餐ID")
        return
    from database import delete_plan
    delete_plan(context.args[0])
    await update.message.reply_text(f"✅ 已删除套餐: {context.args[0]}")

async def cmd_toggle_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config.refresh_config()
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("用法: /toggleplan 套餐ID")
        return
    from database import toggle_plan
    toggle_plan(context.args[0])
    await update.message.reply_text(f"✅ 已切换套餐状态: {context.args[0]}")

async def cmd_add_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config.refresh_config()
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("用法: /addaddr TRC20地址")
        return
    from database import add_address
    add_address(context.args[0])
    await update.message.reply_text(f"✅ 已添加地址: {context.args[0]}")

async def cmd_del_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config.refresh_config()
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("用法: /deladdr TRC20地址")
        return
    from database import delete_address
    delete_address(context.args[0])
    await update.message.reply_text(f"✅ 已删除地址: {context.args[0]}")

# ================= 系统设置 =================
async def admin_settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config.refresh_config()
    """系统设置菜单"""
    query = update.callback_query
    await query.answer()

    from config import refresh_config, get_group_id, get_channel_id, get_trial_hours, get_remind_hours, get_usdt_order_timeout, get_delete_delay

    refresh_config()

    group_id = get_group_id()
    channel_id = get_channel_id()

    text = "⚙️ **系统设置**\n\n"
    text += f"📌 群组ID: `{group_id if group_id else '未设置'}`\n"
    text += f"🔗 邀请链接: `{config.GROUP_LINK if config.GROUP_LINK else '未设置'}`\n"
    text += f"📢 频道ID: `{channel_id if channel_id else '未设置'}`\n"
    text += f"📡 频道链接: `{config.CHANNEL_LINK if config.CHANNEL_LINK else '未设置'}`\n"
    text += f"⏱ 试用时长: {get_trial_hours()} 小时\n"
    text += f"🔔 试用到期提醒: 提前 {get_remind_hours()} 小时\n"
    text += f"📅 会员到期提醒: 提前 {config.MEMBER_REMIND_DAYS} 天\n"
    text += f"⏰ 订单超时: {get_usdt_order_timeout()} 秒\n"
    text += f"🗑 消息删除: {get_delete_delay()} 秒\n\n"
    text += "💡 点击下方按钮修改对应设置："

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📌 设置群组ID", callback_data="admin_set_group"),
         InlineKeyboardButton("🔗 设置群链接", callback_data="admin_set_invite_link")],
        [InlineKeyboardButton("📢 设置频道ID", callback_data="admin_set_channel"),
         InlineKeyboardButton("📡 设置频道链接", callback_data="admin_set_channel_link")],
        [InlineKeyboardButton("⏱ 设置试用时长", callback_data="admin_set_trial"),
         InlineKeyboardButton("🔔 试用提醒", callback_data="admin_set_remind")],
        [InlineKeyboardButton("📅 会员提醒", callback_data="admin_set_member_remind")],
        [InlineKeyboardButton("⏰ 设置订单超时", callback_data="admin_set_timeout"),
         InlineKeyboardButton("🗑 设置删除延迟", callback_data="admin_set_delete")],
        [InlineKeyboardButton("🔄 刷新配置", callback_data="admin_settings")],
        [InlineKeyboardButton("◀️ 返回", callback_data="back_to_admin_menu")],
    ])
    
    try:
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)
    except Exception as e:
        if "not modified" not in str(e):
            raise
# ================= 各项设置的回调 =================
async def admin_set_group_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config.refresh_config()
    query = update.callback_query
    await query.answer()
    context.user_data['waiting_for'] = 'set_group_id'
    await query.edit_message_text(
        "📌 请输入新的群组ID（带 -100 前缀）：\n\n"
        "例如：`-1001234567890`\n\n"
        "回复 /cancel 取消",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ 返回", callback_data="admin_settings")]])
    )
    
async def admin_set_invite_link_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config.refresh_config()
    query = update.callback_query
    await query.answer()
    context.user_data['waiting_for'] = 'set_invite_link'
    await query.edit_message_text(
        "🔗 请输入新的群组邀请链接：\n\n"
        "例如：`https://t.me/+abcdefg12345`\n\n"
        "回复 /cancel 取消",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ 返回", callback_data="admin_settings")]])
    )
    
async def admin_set_channel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config.refresh_config()
    query = update.callback_query
    await query.answer()
    context.user_data['waiting_for'] = 'set_channel_id'
    await query.edit_message_text(
        "📢 请输入新的频道ID（带 -100 前缀）：\n\n"
        "例如：`-1009876543210`\n\n"
        "回复 /cancel 取消",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ 返回", callback_data="admin_settings")]])
    )

async def admin_set_channel_link_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config.refresh_config()
    query = update.callback_query
    await query.answer()
    context.user_data['waiting_for'] = 'set_channel_link'
    await query.edit_message_text(
        "📡 请输入新的频道链接：\n\n"
        "例如：`https://t.me/+xxxxx`\n\n"
        "回复 /cancel 取消",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ 返回", callback_data="admin_settings")]])
    )

async def admin_set_trial_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config.refresh_config()
    query = update.callback_query
    await query.answer()
    context.user_data['waiting_for'] = 'set_trial_hours'
    await query.edit_message_text(
        "⏱ 请输入试用时长（小时）：\n\n"
        "例如：`24` 表示 24 小时\n"
        "`0.0167` 表示 1 分钟",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ 返回", callback_data="admin_settings")]])
    )

async def admin_set_remind_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config.refresh_config()
    query = update.callback_query
    await query.answer()
    context.user_data['waiting_for'] = 'set_remind_hours'
    await query.edit_message_text(
        "🔔 请输入提前提醒时间（小时）：\n\n"
        "例如：`3` 表示提前 3 小时提醒",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ 返回", callback_data="admin_settings")]])
    )

async def admin_set_member_remind_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config.refresh_config()
    query = update.callback_query
    await query.answer()
    context.user_data['waiting_for'] = 'set_member_remind'
    await query.edit_message_text(
        "📅 请输入会员到期提醒天数：\n\n"
        "例如：`3` 表示提前 3 天提醒",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ 返回", callback_data="admin_settings")]])
    )

async def admin_set_timeout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config.refresh_config()
    query = update.callback_query
    await query.answer()
    context.user_data['waiting_for'] = 'set_order_timeout'
    await query.edit_message_text(
        "⏰ 请输入订单超时时间（秒）：\n\n"
        "例如：`600` 表示 10 分钟",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ 返回", callback_data="admin_settings")]])
    )

async def admin_set_delete_delay_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config.refresh_config()
    query = update.callback_query
    await query.answer()
    context.user_data['waiting_for'] = 'set_delete_delay'
    await query.edit_message_text(
        "🗑 请输入消息自动删除延迟（秒）：\n\n"
        "例如：`10` 表示 10 秒后删除",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ 返回", callback_data="admin_settings")]])
    )
