import os
import logging
import sqlite3
import re
import sys
from datetime import datetime
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardRemove,
    InlineQueryResultArticle,
    InputTextMessageContent,
    InlineQueryResultCachedDocument,
    InlineQueryResultCachedVideo
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
    CallbackQueryHandler,
    CallbackContext,
    InlineQueryHandler
)
from telegram.error import BadRequest, Forbidden, TelegramError
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# ==============================================================================
# SECTION 1: CONFIGURATION & DATABASE SETUP
# ==============================================================================
try:
    # --- Bot Configuration ---
    TOKEN = os.getenv('BOT_TOKEN')
    STORAGE_CHANNEL_ID = int(os.getenv('STORAGE_CHANNEL_ID'))
    SERIES_CHANNEL_ID = int(os.getenv('SERIES_CHANNEL_ID'))
    ADMIN_USER_ID = int(os.getenv('ADMIN_USER_ID'))
    BOT_USERNAME = os.getenv('BOT_USERNAME')
    DB_FILE = "filmzy_bot.db"
    CACHE_REFRESH_INTERVAL = 300  # 5 minutes

    # Validate configuration
    if not TOKEN or TOKEN == "your_actual_bot_token_here":
        raise ValueError("❌ Please set BOT_TOKEN environment variable!")

    if STORAGE_CHANNEL_ID >= 0 or SERIES_CHANNEL_ID >= 0:
        raise ValueError("❌ Channel ID must be negative! (e.g., -1001234567890)")

    # Configure logging
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("filmzy_bot.log")
        ]
    )
    logger = logging.getLogger(name)
    logger.info("Starting bot with configuration validated")

    # Conversation states
    CHOOSE_CATEGORY, UPLOAD_TITLE, UPLOAD_FILE = 1, 2, 3
    CHOOSE_UPLOAD_TYPE, UPLOAD_SERIES_FILE = 4, 5
    ADMIN_PANEL, MOVIE_TOOLS, EDIT_MOVIE_ID, EDIT_MOVIE_CHOICE = 6, 7, 8, 9
    EDIT_MOVIE_TITLE, EDIT_MOVIE_CATEGORY, EDIT_MOVIE_FILE = 10, 11, 12
    CONFIRM_DUPLICATE, TITLE_CONFIRMATION = 13, 14
    SERIES_TOOLS, DELETE_SERIES, EDIT_SERIES, EDIT_SERIES_TITLE = 15, 16, 17, 18
    DELETE_MOVIE, CONFIRM_DELETE_MOVIE = 19, 20
    USER_MANAGEMENT, LIST_USERS, ADD_ADMIN, INPUT_ADMIN_ID = 21, 22, 23, 24

    # Initialize database
    def init_db():
        """Initialize database with error handling"""
        try:
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()

            # Create movies table
            c.execute('''CREATE TABLE IF NOT EXISTS movies (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        title TEXT NOT NULL,
                        message_id INTEGER NOT NULL,
                        category TEXT NOT NULL,
                        file_id TEXT,
                        media_type TEXT,
                        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                    )''')

            # Create series table
            c.execute('''CREATE TABLE IF NOT EXISTS series (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        title TEXT NOT NULL,
                        message_id INTEGER NOT NULL,
                        file_id TEXT NOT NULL,
                        media_type TEXT NOT NULL,
                        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                    )''')

            # Create categories table
            c.execute('''CREATE TABLE IF NOT EXISTS categories (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL UNIQUE
                    )''')# Create users table for admin features
            c.execute('''CREATE TABLE IF NOT EXISTS users (
                        user_id INTEGER PRIMARY KEY,
                        username TEXT,
                        first_name TEXT,
                        last_name TEXT,
                        join_date DATETIME DEFAULT CURRENT_TIMESTAMP,
                        is_admin BOOLEAN DEFAULT 0
                    )''')

            # Insert default categories
            default_categories = [
                "Anime", "Animation", "Action", "Horror",
                "Comedy", "Drama", "Sci-Fi", "Fantasy",
                "Thriller", "Documentary", "Other"
            ]
            for category in default_categories:
                try:
                    c.execute("INSERT OR IGNORE INTO categories (name) VALUES (?)", (category,))
                except sqlite3.IntegrityError:
                    pass

            # Add new columns if missing
            c.execute("PRAGMA table_info(movies)")
            columns = [col[1] for col in c.fetchall()]
            if 'file_id' not in columns:
                c.execute("ALTER TABLE movies ADD COLUMN file_id TEXT")
            if 'media_type' not in columns:
                c.execute("ALTER TABLE movies ADD COLUMN media_type TEXT")

            c.execute("PRAGMA table_info(series)")
            columns = [col[1] for col in c.fetchall()]
            if 'file_id' not in columns:
                c.execute("ALTER TABLE series ADD COLUMN file_id TEXT")
            if 'media_type' not in columns:
                c.execute("ALTER TABLE series ADD COLUMN media_type TEXT")

            # Add current admin to users table
            try:
                c.execute(
                    "INSERT OR IGNORE INTO users (user_id, is_admin) VALUES (?, ?)",
                    (ADMIN_USER_ID, 1)
                )
                conn.commit()
            except sqlite3.Error as e:
                logger.error(f"Error adding admin to users: {e}")

            conn.commit()
            logger.info("Database initialized successfully")
            return True
        except sqlite3.Error as e:
            logger.error(f"Database initialization error: {e}")
            return False
        finally:
            if 'conn' in locals():
                conn.close()

    # Initialize database
    if not init_db():
        logger.critical("Database initialization failed! Exiting...")
        sys.exit(1)

    # Movie cache with auto-refresh
    movie_cache = []
    last_cache_refresh = datetime.min

    def refresh_movie_cache():
        """Refresh movie cache from database"""
        global movie_cache, last_cache_refresh
        try:
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute("SELECT title, message_id, category, file_id, media_type FROM movies")
            rows = c.fetchall()
            movie_cache = [
                {
                    'title': row[0],
                    'id': row[1],
                    'category': row[2],
                    'file_id': row[3],
                    'media_type': row[4] or 'document'
                } for row in rows
            ]
            last_cache_refresh = datetime.now()
            logger.info(f"Refreshed movie cache with {len(movie_cache)} movies")
            return True
        except sqlite3.Error as e:
            logger.error(f"Cache refresh error: {e}")
            return False
        finally:
            if 'conn' in locals():
                conn.close()

    # Initial cache load
    refresh_movie_cache()

except Exception as config_error:
    logging.critical(f"CONFIGURATION ERROR: {config_error}")
    print(f"❌ Fatal configuration error: {config_error}")
    sys.exit(1)

# ==============================================================================
# SECTION 2: HELPER FUNCTIONS
# ==============================================================================
def get_main_menu_keyboard(user_id: int):
    """Create main menu keyboard with admin buttons"""
    buttons = [
        [InlineKeyboardButton("🎞️ List Movies", callback_data='list_all')],
        [InlineKeyboardButton("📂 Categories", callback_data='show_categories')],
        [InlineKeyboardButton("🔍 Inline Search", switch_inline_query_current_chat="")]
    ]

    # Admin-only buttons
    if user_id == ADMIN_USER_ID:
        buttons.append([InlineKeyboardButton("⚙️ Admin Panel", callback_data='admin_panel')])
        buttons.append([InlineKeyboardButton("🔄 Refresh Cache", callback_data='refresh_cache')])

    return InlineKeyboardMarkup(buttons)

def get_category_keyboard():
    """Get category selection keyboard"""
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT name FROM categories ORDER BY name")
        categories = [row[0] for row in c.fetchall()]

        keyboard = [
            [InlineKeyboardButton(cat, callback_data=f'cat_{cat}')]
            for cat in categories
        ]
        keyboard.append([InlineKeyboardButton("🏠 Main Menu", callback_data='main_menu')])
        return InlineKeyboardMarkup(keyboard)
    except sqlite3.Error as e:
        logger.error(f"Category keyboard error: {e}")
        return InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data='main_menu')]])
    finally:
        if 'conn' in locals():
            conn.close()

def get_upload_type_keyboard():
    """Get upload type selection keyboard"""
    keyboard = [
        [InlineKeyboardButton("🎬 Movie", callback_data='upload_type_movie')],
        [InlineKeyboardButton("📺 Series", callback_data='upload_type_series')],
        [InlineKeyboardButton("❌ Cancel", callback_data='cancel_upload')]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_admin_panel_keyboard():
    """Get admin panel keyboard"""
    keyboard = [
        [InlineKeyboardButton("🎬 Movie Tools", callback_data='movie_tools')],
        [InlineKeyboardButton("📺 Series Tools", callback_data='series_tools')],
        [InlineKeyboardButton("📊 Statistics", callback_data='admin_stats')],
        [InlineKeyboardButton("🔄 Refresh Cache", callback_data='refresh_cache')],
        [InlineKeyboardButton("👤 User Management", callback_data='user_management')],
        [InlineKeyboardButton("🚫 Close Panel", callback_data='admin_close')]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_movie_tools_keyboard():
    """Get movie tools keyboard"""
    keyboard = [
        [InlineKeyboardButton("✏️ Edit Movie", callback_data='edit_movie')],
        [InlineKeyboardButton("🗑️ Delete Movie", callback_data='delete_movie')],
        [InlineKeyboardButton("🔍 Find Duplicates", callback_data='find_duplicates')],
        [InlineKeyboardButton("🔙 Back", callback_data='admin_back')]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_series_tools_keyboard():
    """Get series tools keyboard"""
    keyboard = [
        [InlineKeyboardButton("✏️ Edit Series", callback_data='edit_series')],
        [InlineKeyboardButton("🗑️ Delete Series", callback_data='delete_series')],
        [InlineKeyboardButton("🔍 List All Series", callback_data='list_series')],
        [InlineKeyboardButton("🔙 Back", callback_data='admin_back')]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_user_management_keyboard():
    """Get user management keyboard"""
    keyboard = [
        [InlineKeyboardButton("👥 List Users", callback_data='list_users')],
        [InlineKeyboardButton("👑 Add Admin", callback_data='add_admin')],
        [InlineKeyboardButton("🔙 Back", callback_data='admin_back')]
    ]
    return InlineKeyboardMarkup(keyboard)def get_confirmation_keyboard():
    """Get confirmation keyboard for deletions"""
    keyboard = [
        [InlineKeyboardButton("✅ Yes, Delete", callback_data='confirm_delete')],
        [InlineKeyboardButton("❌ Cancel", callback_data='cancel_delete')]
    ]
    return InlineKeyboardMarkup(keyboard)

async def send_movie_to_user(context: CallbackContext, user_id: int, movie_id: int, chat_id: int = None):
    """Send movie to user with error handling"""
    try:
        # Find movie in cache
        movie = next((m for m in movie_cache if m['id'] == movie_id), None)

        if not movie:
            logger.error(f"Movie {movie_id} not found in cache")
            return False

        # Try to send using file_id
        if movie.get('file_id'):
            try:
                if movie['media_type'] == 'video':
                    await context.bot.send_video(
                        chat_id=user_id,
                        video=movie['file_id'],
                        caption=f"🎬 {movie['title']} ({movie['category']})"
                    )
                else:
                    await context.bot.send_document(
                        chat_id=user_id,
                        document=movie['file_id'],
                        caption=f"🎬 {movie['title']} ({movie['category']})"
                    )
                logger.info(f"Sent movie {movie_id} to user {user_id} via file_id")
                return True
            except Exception as e:
                logger.warning(f"File_id send failed: {e}. Falling back to forwarding")

        # Fallback to message forwarding
        try:
            await context.bot.forward_message(
                chat_id=user_id,
                from_chat_id=STORAGE_CHANNEL_ID,
                message_id=movie_id
            )
            logger.info(f"Forwarded movie {movie_id} to user {user_id}")
            return True
        except (BadRequest, Forbidden) as e:
            logger.error(f"Forward error: {str(e)}")
            if chat_id:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="❌ Please start a private chat with me first: "
                    f"https://t.me/{BOT_USERNAME}"
                )
            return False
    except Exception as e:
        logger.error(f"Send movie error: {str(e)}")
        if chat_id:
            await context.bot.send_message(
                chat_id=chat_id,
                text="❌ Failed to send movie. Please try again later."
            )
        return False

def add_movie_to_db(title: str, message_id: int, category: str, file_id: str = None, media_type: str = 'document'):
    """Add movie to database"""
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute(
            "INSERT INTO movies (title, message_id, category, file_id, media_type) VALUES (?, ?, ?, ?, ?)",
            (title, message_id, category, file_id, media_type)
        )
        conn.commit()

        # Add category if not exists
        c.execute("INSERT OR IGNORE INTO categories (name) VALUES (?)", (category,))
        conn.commit()

        logger.info(f"Added movie to DB: {title} (ID: {message_id}) in {category} as {media_type}")
        return True
    except sqlite3.Error as e:
        logger.error(f"Database insert error: {e}")
        return False
    finally:
        if 'conn' in locals():
            conn.close()

def add_series_to_db(title: str, message_id: int, file_id: str, media_type: str):
    """Add series to database"""
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute(
            "INSERT INTO series (title, message_id, file_id, media_type) VALUES (?, ?, ?, ?)",
            (title, message_id, file_id, media_type)
        )conn.commit()
        logger.info(f"Added series to DB: {title} (ID: {message_id}) as {media_type}")
        return True
    except sqlite3.Error as e:
        logger.error(f"Series database insert error: {e}")
        return False
    finally:
        if 'conn' in locals():
            conn.close()

def update_movie_in_db(movie_id: int, field: str, value: str):
    """Update movie in database"""
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()

        if field == 'title':
            c.execute("UPDATE movies SET title = ? WHERE message_id = ?", (value, movie_id))
        elif field == 'category':
            c.execute("UPDATE movies SET category = ? WHERE message_id = ?", (value, movie_id))
        elif field == 'file':
            c.execute("UPDATE movies SET file_id = ?, media_type = ? WHERE message_id = ?",
                      (value['file_id'], value['media_type'], movie_id))

        conn.commit()
        logger.info(f"Updated movie {movie_id}: {field} = {value if field != 'file' else 'FILE'}")
        return True
    except sqlite3.Error as e:
        logger.error(f"Database update error: {e}")
        return False
    finally:
        if 'conn' in locals():
            conn.close()

def delete_movie_from_db(movie_id: int):
    """Delete movie from database"""
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("DELETE FROM movies WHERE message_id = ?", (movie_id,))
        conn.commit()
        logger.info(f"Deleted movie from DB: ID {movie_id}")
        return True
    except sqlite3.Error as e:
        logger.error(f"Delete movie error: {e}")
        return False
    finally:
        if 'conn' in locals():
            conn.close()

def delete_series_from_db(series_id: int):
    """Delete series from database"""
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("DELETE FROM series WHERE message_id = ?", (series_id,))
        conn.commit()
        logger.info(f"Deleted series from DB: ID {series_id}")
        return True
    except sqlite3.Error as e:
        logger.error(f"Delete series error: {e}")
        return False
    finally:
        if 'conn' in locals():
            conn.close()

def find_duplicate_movies():
    """Find duplicate movies in database"""
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('''
            SELECT title, COUNT(*) as count
            FROM movies
            GROUP BY title
            HAVING count > 1
        ''')
        duplicates = c.fetchall()
        return duplicates
    except sqlite3.Error as e:
        logger.error(f"Find duplicates error: {e}")
        return []

def get_bot_statistics():
    """Get comprehensive bot statistics"""
    stats = {}
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()

        # Movie statistics
        c.execute("SELECT COUNT(*) FROM movies")
        stats['total_movies'] = c.fetchone()[0]

        c.execute("SELECT COUNT(DISTINCT category) FROM movies")
        stats['total_categories'] = c.fetchone()[0]

        # Series statistics
        c.execute("SELECT COUNT(*) FROM series")
        stats['total_series'] = c.fetchone()[0]

        # User statistics
        c.execute("SELECT COUNT(*) FROM users")
        stats['total_users'] = c.fetchone()[0]

        c.execute("SELECT COUNT(*) FROM users WHERE is_admin = 1")
        stats['admin_users'] = c.fetchone()[0]

        # Recent activity
        c.execute("SELECT MAX(timestamp) FROM movies")
        stats['last_movie_added'] = c.fetchone()[0]

        c.execute("SELECT MAX(timestamp) FROM series")
        stats['last_series_added'] = c.fetchone()[0]

        return stats
    except sqlite3.Error as e:
        logger.error(f"Statistics error: {e}")
        return {}def get_all_series():
    """Get all series from database"""
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT title, message_id FROM series ORDER BY title")
        series_list = c.fetchall()
        return series_list
    except sqlite3.Error as e:
        logger.error(f"Get series error: {e}")
        return []

def get_all_users():
    """Get all users from database"""
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT user_id, username, first_name, last_name, is_admin FROM users")
        users = c.fetchall()
        return users
    except sqlite3.Error as e:
        logger.error(f"Get users error: {e}")
        return []

def add_admin_user(user_id: int):
    """Add or update user as admin"""
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute(
            "INSERT OR REPLACE INTO users (user_id, is_admin) VALUES (?, ?)",
            (user_id, 1)
        )
        conn.commit()
        logger.info(f"Added admin user: {user_id}")
        return True
    except sqlite3.Error as e:
        logger.error(f"Add admin error: {e}")
        return False
    finally:
        if 'conn' in locals():
            conn.close()

      # ==============================================================================
# SECTION 3: COMMAND HANDLERS
# ==============================================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    user = update.effective_user
    user_id = user.id
    
    # Add user to database
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute(
            "INSERT OR IGNORE INTO users (user_id, username, first_name, last_name) VALUES (?, ?, ?, ?)",
            (user_id, user.username, user.first_name, user.last_name)
        )
        conn.commit()
    except sqlite3.Error as e:
        logger.error(f"User database error: {e}")
    
    welcome_text = (
        "🎬 Welcome to FilmzyZone Bot!\n\n"
        "I can help you find and download movies and series.\n\n"
        "🔍 How to use:\n"
        "• Use inline search: type @filmzyzonebot in any chat\n"
        "• Browse categories\n"
        "• List all movies\n"
        "• Upload your own content\n\n"
        "Choose an option below:"
    )
    
    await update.message.reply_text(
        welcome_text,
        reply_markup=get_main_menu_keyboard(user_id),
        parse_mode='Markdown'
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text messages"""
    user_id = update.effective_user.id
    text = update.message.text
    
    if text.startswith('/'):
        return
    
    # Search functionality
    if len(text) >= 2:
        await search_and_send_movies(update, context, text)
    else:
        await update.message.reply_text(
            "Please enter at least 2 characters to search.",
            reply_markup=get_main_menu_keyboard(user_id)
        )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle button callbacks"""
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    data = query.data
    
    # Handle different button actions
    if data == 'main_menu':
        await query.edit_message_text(
            "🏠 Main Menu",
            reply_markup=get_main_menu_keyboard(user_id)
        )
    
    elif data == 'show_categories':
        await query.edit_message_text(
            "📂 Choose a category:",
            reply_markup=get_category_keyboard()
        )
    
    elif data.startswith('cat_'):
        category = data[4:]
        await show_category_movies(query, category)
    
    elif data == 'list_all':
        await list_all_movies(query)
    
    elif data == 'admin_panel' and user_id == ADMIN_USER_ID:
        await query.edit_message_text(
            "⚙️ Admin Panel",
            reply_markup=get_admin_panel_keyboard()
        )
    
    elif data == 'refresh_cache' and user_id == ADMIN_USER_ID:
        refresh_movie_cache()
        await query.edit_message_text(
            "✅ Cache refreshed successfully!",
            reply_markup=get_main_menu_keyboard(user_id)
        )
    
    elif data == 'movie_tools' and user_id == ADMIN_USER_ID:
        await query.edit_message_text(
            "🎬 Movie Management Tools",
            reply_markup=get_movie_tools_keyboard()
        )
    
    elif data == 'admin_back':
        await query.edit_message_text(
            "⚙️ Admin Panel",
            reply_markup=get_admin_panel_keyboard()
        )
    
    else:
        await query.edit_message_text(
            "❌ Unknown command",
            reply_markup=get_main_menu_keyboard(user_id)
        )

async def show_category_movies(query, category):
    """Show movies in a specific category"""
    # Refresh cache if needed
    if (datetime.now() - last_cache_refresh).seconds > CACHE_REFRESH_INTERVAL:refresh_movie_cache()
    
    category_movies = [m for m in movie_cache if m['category'].lower() == category.lower()]
    
    if not category_movies:
        await query.edit_message_text(
            f"❌ No movies found in category: {category}",
            reply_markup=get_category_keyboard()
        )
        return
    
    movie_list = "\n".join([f"• {movie['title']}" for movie in category_movies[:20]])
    
    if len(category_movies) > 20:
        movie_list += f"\n\n... and {len(category_movies) - 20} more movies"
    
    await query.edit_message_text(
        f"🎬 Movies in {category} ({len(category_movies)} total):\n\n{movie_list}",
        reply_markup=get_category_keyboard(),
        parse_mode='Markdown'
    )

async def list_all_movies(query):
    """List all movies"""
    # Refresh cache if needed
    if (datetime.now() - last_cache_refresh).seconds > CACHE_REFRESH_INTERVAL:
        refresh_movie_cache()
    
    if not movie_cache:
        await query.edit_message_text(
            "❌ No movies available yet!",
            reply_markup=get_main_menu_keyboard(query.from_user.id)
        )
        return
    
    total_movies = len(movie_cache)
    categories = set(movie['category'] for movie in movie_cache)
    
    await query.edit_message_text(
        f"📊 Movie Library Stats:\n\n"
        f"• Total Movies: {total_movies}\n"
        f"• Categories: {len(categories)}\n"
        f"• Last Updated: {last_cache_refresh.strftime('%Y-%m-%d %H:%M')}\n\n"
        f"Use the search feature or browse by categories to find movies!",
        reply_markup=get_main_menu_keyboard(query.from_user.id),
        parse_mode='Markdown'
    )

async def search_and_send_movies(update: Update, context: ContextTypes.DEFAULT_TYPE, query: str):
    """Search and send movies to user"""
    # Refresh cache if needed
    if (datetime.now() - last_cache_refresh).seconds > CACHE_REFRESH_INTERVAL:
        refresh_movie_cache()
    
    # Search in cache
    results = []
    search_terms = query.lower().split()
    
    for movie in movie_cache:
        title_lower = movie['title'].lower()
        if any(term in title_lower for term in search_terms):
            results.append(movie)
    
    if not results:
        await update.message.reply_text(
            f"❌ No movies found for '{query}'",
            reply_markup=get_main_menu_keyboard(update.effective_user.id)
        )
        return
    
    # Send results
    await update.message.reply_text(f"🎭 Found {len(results)} movies for '{query}':")
    
    sent_count = 0
    for movie in results[:10]:  # Limit to 10 results
        success = await send_movie_to_user(
            context, 
            update.effective_user.id, 
            movie['id'],
            update.effective_chat.id
        )
        if success:
            sent_count += 1
    
    if sent_count == 0:
        await update.message.reply_text(
            "❌ Could not send any movies. Please start a private chat with me.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "💬 Start Private Chat", 
                    url=f"https://t.me/{BOT_USERNAME}?start=private"
                )
            ]])
        )

async def inline_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline queries"""
    query = update.inline_query.query
    
    if not query or len(query) < 2:
        return
    
    # Refresh cache if needed
    if (datetime.now() - last_cache_refresh).seconds > CACHE_REFRESH_INTERVAL:
        refresh_movie_cache()
    
    # Search in cache
    results = []
    search_terms = query.lower().split()
    
    for movie in movie_cache:
        title_lower = movie['title'].lower()
        if any(term in title_lower for term in search_terms):results.append(movie)
    
    # Create inline results
    inline_results = []
    for movie in results[:50]:  # Telegram limit
        if movie['media_type'] == 'video':
            result = InlineQueryResultCachedVideo(
                id=str(movie['id']),
                video_file_id=movie['file_id'],
                title=movie['title'],
                caption=f"🎬 {movie['title']} ({movie['category']})",
                description=f"Category: {movie['category']}"
            )
        else:
            result = InlineQueryResultCachedDocument(
                id=str(movie['id']),
                document_file_id=movie['file_id'],
                title=movie['title'],
                caption=f"🎬 {movie['title']} ({movie['category']})",
                description=f"Category: {movie['category']}"
            )
        inline_results.append(result)
    
    await update.inline_query.answer(inline_results, cache_time=300)

def main():
    """Start the bot"""
    # Create application
    application = Application.builder().token(TOKEN).build()
    
    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(InlineQueryHandler(inline_query_handler))
    
    # Start bot
    logger.info("Bot starting...")
    application.run_polling()
    logger.info("Bot stopped")

if name == 'main':
    main()
