# database.py - 完整修复版本
import os
import logging
import sqlite3
import threading
from datetime import datetime, timedelta
from typing import Optional, Tuple

import pytz

BEIJING = pytz.timezone("Asia/Shanghai")

DB_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(DB_DIR, "vip.db")

# ================= 线程安全的数据库连接 =================
_thread_local = threading.local()

def get_db_connection():
    """获取当前线程的数据库连接"""
    if not hasattr(_thread_local, "connection"):
        _thread_local.connection = sqlite3.connect(
            DB_PATH,
            check_same_thread=False,
            isolation_level=None
        )
        _thread_local.connection.row_factory = sqlite3.Row
    return _thread_local.connection

def db_execute(sql, args=()):
    """执行SQL语句（线程安全）"""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(sql, args)
    conn.commit()
    return cur

def now():
    return datetime.now(BEIJING)

# ================= 初始化数据库表 =================
def init_db():
    """初始化所有数据库表"""

    # ================= 套餐表 =================
    db_execute("""
        CREATE TABLE IF NOT EXISTS vip_plans (
            plan_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            days INTEGER NOT NULL,
            price REAL NOT NULL,
            is_active INTEGER DEFAULT 1
        )
    """)
    # 套餐由管理员通过命令手动添加
    # /addplan 套餐ID 名称 天数 价格

    # ================= 地址池表 =================
    db_execute("""
        CREATE TABLE IF NOT EXISTS vip_addresses (
            address TEXT PRIMARY KEY,
            status TEXT DEFAULT 'idle',
            added_at TEXT
        )
    """)

    # ================= 用户表 =================
    db_execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        expire_time TEXT,
        is_permanent INTEGER DEFAULT 0,
        trial_start_time TEXT,
        trial_reminded INTEGER DEFAULT 0,
        is_banned INTEGER DEFAULT 0
    )
    """)
    logging.info("数据库表 users 已初始化")

    # ================= 封禁表 =================
    db_execute("""
    CREATE TABLE IF NOT EXISTS banned (
        user_id INTEGER PRIMARY KEY,
        reason TEXT,
        banned_at TEXT
    )
    """)

    # ================= 管理员日志表 =================
    db_execute("""
    CREATE TABLE IF NOT EXISTS admin_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        admin_id INTEGER,
        action TEXT,
        target_id INTEGER,
        timestamp TEXT
    )
    """)

    # ================= 消息记录表 =================
    db_execute("""
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        from_user INTEGER,
        to_user INTEGER,
        message TEXT,
        timestamp TEXT,
        is_reply INTEGER DEFAULT 0
    )
    """)

    # ================= USDT 交易记录表 =================
    db_execute("""
    CREATE TABLE IF NOT EXISTS processed_transactions (
        tx_id TEXT PRIMARY KEY,
        user_id INTEGER,
        days INTEGER,
        processed_at TEXT
    )
    """)
    logging.info("USDT 交易记录表已初始化")

    # ================= USDT 订单表 =================
    db_execute("""
    CREATE TABLE IF NOT EXISTS usdt_orders (
        order_id TEXT PRIMARY KEY,
        user_id INTEGER,
        plan_name TEXT,
        days INTEGER,
        amount REAL,
        status TEXT,
        created_at TEXT,
        paid_at TEXT,
        tx_id TEXT
    )
    """)
    logging.info("USDT 订单记录表已初始化")

    # ================= 分布式锁表 =================
    db_execute("""
        CREATE TABLE IF NOT EXISTS scheduler_locks (
            lock_name TEXT PRIMARY KEY,
            acquired_at REAL,
            expires_at REAL,
            worker_id TEXT
        )
    """)

    # ================= 添加缺失的列 =================
    # 🔧 修复：address 字段放在最前面
    try:
        db_execute("ALTER TABLE usdt_orders ADD COLUMN address TEXT DEFAULT ''")
        logging.info("已添加 usdt_orders.address 字段")
    except sqlite3.OperationalError:
        pass

    columns_to_add = [
        ("users", "last_channel_check", "TEXT"),
        ("users", "needs_channel_check", "INTEGER DEFAULT 0"),
        ("users", "reminded_type", "TEXT DEFAULT NULL"),
    ]

    for table, column, col_type in columns_to_add:
        try:
            db_execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
            logging.info(f"已添加列 {table}.{column}")
        except sqlite3.OperationalError:
            pass

    logging.info("数据库表结构已更新")

    # ================= 系统设置表 =================
    db_execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    logging.info("系统设置表已初始化")

    # ================= 创建索引 =================
    indexes = [
        ("idx_orders_status_created", "usdt_orders", "status, created_at"),
        ("idx_orders_plan_name", "usdt_orders", "plan_name"),
        ("idx_orders_user_id", "usdt_orders", "user_id"),
        ("idx_users_expire", "users", "expire_time"),
        ("idx_users_banned", "users", "is_banned"),
        ("idx_users_permanent", "users", "is_permanent"),
        ("idx_messages_timestamp", "messages", "timestamp"),
        ("idx_messages_users", "messages", "from_user, to_user"),
    ]

    for idx_name, table, columns in indexes:
        try:
            db_execute(f"CREATE INDEX IF NOT EXISTS {idx_name} ON {table}({columns})")
        except Exception as e:
            logging.warning(f"创建索引 {idx_name} 时出错: {e}")

    logging.info("数据库索引创建完成")


# ================= 辅助函数 =================
def get_pending_orders():
    """获取所有待处理的订单"""
    rows = db_execute("""
        SELECT order_id, user_id, plan_name, days, amount, created_at, address
        FROM usdt_orders WHERE status='pending'
    """).fetchall()
    return rows


def is_admin(user_id: int) -> bool:
    from config import ADMIN_ID
    return user_id == ADMIN_ID


def get_user(user_id: int):
    return db_execute(
        "SELECT expire_time, is_permanent, trial_start_time, is_banned FROM users WHERE user_id=?",
        (user_id,)
    ).fetchone()


def get_user_status(user_id: int) -> Tuple[bool, str]:
    """返回 (是否有效, 状态描述)"""
    import config
    config.refresh_config()
    
    row = get_user(user_id)
    if not row:
        return False, "未获得试用资格"

    # 优先级1: 永久会员
    if row[1] == 1:
        return True, "永久会员"

    # 优先级2: 付费会员
    if row[0]:  # expire_time
        try:
            expire = datetime.fromisoformat(row[0]).astimezone(BEIJING)
            if expire > now():
                days_left = (expire - now()).days
                if days_left > 0:
                    return True, f"会员剩余 {days_left} 天"
                else:
                    hours_left = (expire - now()).seconds // 3600
                    return True, f"会员剩余 {hours_left} 小时"
            else:
                return False, "会员已到期，请续费"
        except Exception as e:
            logging.error(f"解析用户 {user_id} expire_time 失败: {e}, 值: {row[0]}")
            return False, "会员已到期，请续费"

    # 优先级3: 检查是否有过付费记录（已到期）
    # 🔧 通过 usdt_orders 表判断是否曾经购买过
    has_paid = db_execute(
        "SELECT 1 FROM usdt_orders WHERE user_id=? AND status='paid' LIMIT 1",
        (user_id,)
    ).fetchone()

    # 优先级4: 试用用户
    if row[2]:  # trial_start_time
        try:
            trial_start = datetime.fromisoformat(row[2]).astimezone(BEIJING)
            trial_end = trial_start + timedelta(hours=config.TRIAL_HOURS)
            if trial_end > now():
                return True, f"试用剩余 {(trial_end-now()).seconds//3600} 小时"
            else:
                if has_paid:
                    return False, "会员已到期，请续费"
                else:
                    return False, "试用已到期，请购买会员"
        except Exception as e:
            logging.error(f"解析用户 {user_id} trial_start_time 失败: {e}, 值: {row[2]}")
            return False, "试用已到期，请购买会员"

    # 优先级5: 被封禁
    if row[3] == 1:
        if has_paid:
            return False, "会员已到期，请续费"
        else:
            return False, "试用已到期，请购买会员"

    return False, "未获得试用资格"


def has_valid_membership(user_id: int) -> bool:
    is_valid, _ = get_user_status(user_id)
    return is_valid


def add_trial(user_id: int):
    db_execute("""
        INSERT OR IGNORE INTO users (user_id, trial_start_time, trial_reminded, is_banned)
        VALUES (?, ?, 0, 0)
    """, (user_id, now().isoformat()))


def add_permanent(user_id: int):
    db_execute("""
        INSERT INTO users (user_id, is_permanent, expire_time, trial_start_time, is_banned)
        VALUES (?, 1, NULL, NULL, 0)
        ON CONFLICT(user_id) DO UPDATE SET 
            is_permanent=1, expire_time=NULL, trial_start_time=NULL, is_banned=0
    """, (user_id,))


def remove_permanent(user_id: int):
    db_execute("""
        UPDATE users SET is_permanent=0, expire_time=NULL, trial_start_time=NULL, is_banned=1
        WHERE user_id=?
    """, (user_id,))


def extend_member(user_id: int, days: int):
    row = db_execute("SELECT expire_time FROM users WHERE user_id=?", (user_id,)).fetchone()
    current = now()
    if row and row[0]:
        old_expire = datetime.fromisoformat(row[0]).astimezone(BEIJING)
        new_expire = max(old_expire, current) + timedelta(days=days)
    else:
        new_expire = current + timedelta(days=days)
    db_execute("""
        INSERT INTO users (user_id, expire_time, is_permanent, trial_start_time, is_banned, trial_reminded)
        VALUES (?, ?, 0, NULL, 0, 0)
        ON CONFLICT(user_id) DO UPDATE SET 
            expire_time=excluded.expire_time, 
            is_permanent=0, trial_start_time=NULL, is_banned=0, trial_reminded=0
    """, (user_id, new_expire.isoformat()))
    return new_expire


def ban_user(user_id: int, reason: str):
    db_execute("UPDATE users SET is_banned=1 WHERE user_id=?", (user_id,))
    db_execute("INSERT OR IGNORE INTO banned (user_id, reason, banned_at) VALUES (?,?,?)",
               (user_id, reason, now().isoformat()))


def unban_user(user_id: int):
    db_execute("UPDATE users SET is_banned=0 WHERE user_id=?", (user_id,))
    db_execute("DELETE FROM banned WHERE user_id=?", (user_id,))


def delete_user_membership(user_id: int):
    db_execute("""
        UPDATE users SET expire_time=NULL, is_permanent=0, trial_start_time=NULL, is_banned=1
        WHERE user_id=?
    """, (user_id,))


async def is_user_following_channel(context, user_id: int) -> Optional[bool]:
    import config
    try:
        member = await context.bot.get_chat_member(chat_id=config.CHANNEL_ID, user_id=user_id)
        return member.status in ["member", "administrator", "creator"]
    except Exception as e:
        logging.error(f"检查用户 {user_id} 频道关注状态失败: {e}")
        return None


def save_message(from_user: int, to_user: int, message: str):
    db_execute("""
        INSERT INTO messages (from_user, to_user, message, timestamp)
        VALUES (?, ?, ?, ?)
    """, (from_user, to_user, message, now().isoformat()))


def log_admin_action(admin_id: int, action: str, target_id: int = None):
    db_execute("""
        INSERT INTO admin_logs (admin_id, action, target_id, timestamp)
        VALUES (?, ?, ?, ?)
    """, (admin_id, action, target_id, now().isoformat()))


# ================= 订单清理函数 =================
def get_retention_days(plan_name: str) -> int:
    """根据套餐名称返回保留天数"""
    retention_map = {
        "1个月会员": 35,
        "3个月会员": 100,
        "6个月会员": 200,
        "1年会员": 400,
    }
    if plan_name in retention_map:
        return retention_map[plan_name]
    if "1个月" in plan_name:
        return 35
    elif "3个月" in plan_name:
        return 100
    elif "6个月" in plan_name:
        return 200
    elif "1年" in plan_name or "一年" in plan_name:
        return 400
    return 90


def clean_old_orders():
    """根据套餐类型清理不同期限的订单"""
    current = now()
    deleted_count = 0
    expired_cutoff = (current - timedelta(days=7)).isoformat()
    expired_deleted = db_execute("""
        DELETE FROM usdt_orders WHERE status = 'expired' AND created_at < ?
    """, (expired_cutoff,)).rowcount
    deleted_count += expired_deleted
    cancelled_cutoff = (current - timedelta(days=7)).isoformat()
    cancelled_deleted = db_execute("""
        DELETE FROM usdt_orders WHERE status = 'cancelled' AND created_at < ?
    """, (cancelled_cutoff,)).rowcount
    deleted_count += cancelled_deleted
    paid_orders = db_execute("""
        SELECT order_id, plan_name, created_at FROM usdt_orders WHERE status = 'paid'
    """).fetchall()
    for order_id, plan_name, created_at_str in paid_orders:
        created_at = datetime.fromisoformat(created_at_str)
        retention_days = get_retention_days(plan_name)
        if retention_days:
            cutoff_date = created_at + timedelta(days=retention_days)
            if current > cutoff_date:
                db_execute("DELETE FROM usdt_orders WHERE order_id=?", (order_id,))
                deleted_count += 1
    logging.info(f"总计清理订单: {deleted_count} 条")
    return deleted_count


def update_expired_pending_orders():
    """将超时的待处理订单标记为过期，并释放地址"""
    from config import USDT_ORDER_TIMEOUT
    timeout_seconds = USDT_ORDER_TIMEOUT
    cutoff_time = (now() - timedelta(seconds=timeout_seconds)).isoformat()
    # 先获取要过期的订单地址
    expired_orders = db_execute("""
        SELECT order_id, address FROM usdt_orders 
        WHERE status = 'pending' AND created_at < ?
    """, (cutoff_time,)).fetchall()
    updated = db_execute("""
        UPDATE usdt_orders SET status = 'expired' 
        WHERE status = 'pending' AND created_at < ?
    """, (cutoff_time,)).rowcount
    # 释放地址
    for order_id, address in expired_orders:
        if address:
            still_in_use = db_execute("""
                SELECT COUNT(*) FROM usdt_orders 
                WHERE address = ? AND status = 'pending'
            """, (address,)).fetchone()[0]
            if still_in_use == 0:
                mark_address_idle(address)
    if updated > 0:
        logging.info(f"已将 {updated} 个超时待处理订单标记为过期并释放地址")
    return updated


# ================= 套餐管理函数 =================
def get_active_plans():
    rows = db_execute("SELECT plan_id, name, days, price FROM vip_plans WHERE is_active=1 ORDER BY price").fetchall()
    return [{"plan_id": r[0], "name": r[1], "days": r[2], "price": r[3]} for r in rows]


def get_all_plans():
    rows = db_execute("SELECT plan_id, name, days, price, is_active FROM vip_plans ORDER BY plan_id").fetchall()
    return [{"plan_id": r[0], "name": r[1], "days": r[2], "price": r[3], "is_active": r[4]} for r in rows]


def add_plan(plan_id: str, name: str, days: int, price: float):
    db_execute("INSERT OR IGNORE INTO vip_plans (plan_id, name, days, price) VALUES (?, ?, ?, ?)",
               (plan_id, name, days, price))


def delete_plan(plan_id: str):
    db_execute("DELETE FROM vip_plans WHERE plan_id=?", (plan_id,))


def toggle_plan(plan_id: str):
    row = db_execute("SELECT is_active FROM vip_plans WHERE plan_id=?", (plan_id,)).fetchone()
    if row:
        new_state = 0 if row[0] else 1
        db_execute("UPDATE vip_plans SET is_active=? WHERE plan_id=?", (new_state, plan_id))


# ================= 地址管理函数 =================
def get_available_address():
    row = db_execute("SELECT address FROM vip_addresses WHERE status='idle' LIMIT 1").fetchone()
    return row[0] if row else None


def mark_address_used(address: str):
    db_execute("UPDATE vip_addresses SET status='used' WHERE address=?", (address,))


def mark_address_idle(address: str):
    db_execute("UPDATE vip_addresses SET status='idle' WHERE address=?", (address,))


def add_address(address: str):
    db_execute("INSERT OR IGNORE INTO vip_addresses (address, status, added_at) VALUES (?, 'idle', ?)",
               (address, now().isoformat()))


def delete_address(address: str):
    db_execute("DELETE FROM vip_addresses WHERE address=?", (address,))


def get_all_users_for_broadcast():
    """获取所有用户（包括封禁的），用于广播"""
    return db_execute("SELECT user_id FROM users").fetchall()
