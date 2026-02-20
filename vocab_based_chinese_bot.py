import os
import logging
import random
import json
import io
import tempfile
from typing import List, Dict, Optional
import re
from dotenv import load_dotenv
load_dotenv()

import telebot
from telebot import types
from openai import OpenAI
from google.cloud import texttospeech
from google.oauth2 import service_account
import langdetect

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Environment variables
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
DEEPSEEK_API_KEY = os.getenv('DEEPSEEK_API_KEY')
GOOGLE_CREDS_PATH = os.getenv('GOOGLE_CREDS_PATH', '/home/user/google-creds.json')

# Initialize bot
bot = telebot.TeleBot(TELEGRAM_TOKEN)

# Initialize DeepSeek client
deepseek_client = OpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url="https://api.deepseek.com"
)

# Initialize Google TTS
credentials = service_account.Credentials.from_service_account_file(GOOGLE_CREDS_PATH)
tts_client = texttospeech.TextToSpeechClient(credentials=credentials)

# Voice configurations
CHIRP_VOICES = {
    'en': [
        "en-US-Chirp3-HD-Achird",
        "en-US-Chirp3-HD-Callirrhoe",
        "en-US-Chirp3-HD-Achernar",
        "en-US-Chirp3-HD-Algenib",
        "en-US-Chirp3-HD-Erinome",
        "en-US-Chirp3-HD-Schedar",
        "en-US-Chirp3-HD-Kore"
    ],
    'es': [
        "es-ES-Chirp-HD-F",
        "es-ES-Chirp-HD-O",
        "es-ES-Chirp3-HD-Gacrux",
        "es-US-Chirp3-HD-Leda",
        "es-ES-Chirp3-HD-Algenib",
        "es-ES-Chirp3-HD-Charon",
        "es-US-Chirp3-HD-Algieba"
    ],
    'zh': [
        "cmn-CN-Chirp3-HD-Aoede",
        "cmn-CN-Chirp3-HD-Leda",
        "cmn-CN-Chirp3-HD-Puck"
    ]
}

# Word count by level
WORD_COUNTS = {
    'C2': 500,
    'C1': 400,
    'B2': 300,
    'B1': 250,
    'A2': 150,
    'A1': 50
}

# Audio speed by level
AUDIO_SPEEDS = {
    'C2': 1.0,   # 100%
    'C1': 1.0,   # 100%
    'B2': 0.90,  # 90%
    'B1': 0.90,  # 90%
    'A2': 0.70,  # 70%
    'A1': 0.70   # 70%
}

# Store user sessions
user_sessions = {}

# Common function words to filter out (basic list)
FILTER_WORDS = {
    'en': {'a', 'an', 'the', 'in', 'on', 'at', 'to', 'for', 'of', 'with', 'by', 'from',
           'have', 'has', 'had', 'be', 'is', 'are', 'was', 'were', 'been', 'being',
           'do', 'does', 'did', 'will', 'would', 'should', 'could', 'can', 'may', 
           'might', 'must', 'shall'},
    'es': {'el', 'la', 'los', 'las', 'un', 'una', 'de', 'en', 'a', 'por', 'para',
           'con', 'sin', 'ser', 'estar', 'haber', 'tener', 'hacer', 'poder', 'deber'},
    'zh': {'的', '了', '在', '是', '我', '有', '和', '人', '这', '中', '大', '为', '上', '个', '国'}
}


class UserSession:
    def __init__(self):
        self.words: List[str] = []
        self.language: Optional[str] = None
        self.level: Optional[str] = None
        self.topic: Optional[str] = None
        self.raw_text: Optional[str] = None
        self.awaiting_column: bool = False
        self.awaiting_confirmation: bool = False


def detect_language(words: List[str]) -> str:
    """Detect language from word list"""
    sample_text = ' '.join(words[:10])
    try:
        lang_code = langdetect.detect(sample_text)
        if lang_code in ['en']:
            return 'en'
        elif lang_code in ['es']:
            return 'es'
        elif lang_code in ['zh-cn', 'zh-tw']:
            return 'zh'
        else:
            return 'en'  # default
    except:
        return 'en'


def filter_words(words: List[str], language: str) -> List[str]:
    """Filter out common function words and short words"""
    filter_set = FILTER_WORDS.get(language, set())
    filtered = []
    
    for word in words:
        word_clean = word.strip().lower()
        # Skip if empty, too short (unless Chinese), or in filter list
        if not word_clean:
            continue
        if language != 'zh' and len(word_clean) <= 2:
            continue
        if word_clean in filter_set:
            continue
        filtered.append(word.strip())
    
    return filtered


def parse_anki_export(text: str) -> List[str]:
    """Parse Anki export format and extract first column"""
    lines = text.split('\n')
    words = []
    
    for line in lines:
        line = line.strip()
        if not line or line.startswith('#') or 'Anki' in line or 'http' in line:
            continue
        
        parts = line.split('\t')
        if parts:
            words.append(parts[0].strip())
    
    return words


def parse_column(text: str, column_num: int) -> List[str]:
    """Parse specific column from AnkiDroid or tab-delimited text.
    Handles BOM, #separator headers, tabs, and multi-space delimiters."""
    # Remove BOM if present
    text = text.lstrip('\ufeff')
    lines = text.split('\n')
    words = []

    # Detect delimiter from Anki header if present
    delimiter = '\t'  # default
    for line in lines[:5]:
        ls = line.strip()
        if ls.startswith('#separator:'):
            sep_value = ls.split(':', 1)[1].strip().lower()
            if sep_value == 'tab':
                delimiter = '\t'
            elif sep_value == 'space':
                delimiter = ' '
            break

    # Auto-detect if no header: check first real data line
    else:
        for line in lines:
            ls = line.strip()
            if not ls or ls.startswith('#'):
                continue
            if '\t' in ls:
                delimiter = '\t'
            else:
                delimiter = 'spaces'  # 2+ spaces fallback
            break

    for line in lines:
        line = line.strip()
        if not line or line.startswith('#'):
            continue

        if delimiter == 'spaces':
            parts = re.split(r'  +', line)
        else:
            parts = line.split(delimiter)

        parts = [p.strip() for p in parts]

        if len(parts) >= column_num:
            word = parts[column_num - 1].strip()
            if word:
                words.append(word)
        else:
            logger.debug(f"Line has {len(parts)} cols (need {column_num}): {line[:60]}")

    logger.info(f"parse_column: {len(words)} words from col {column_num}, delimiter={repr(delimiter)}")
    return words


def generate_text_with_vocab(words: List[str], topic: str, level: str, language: str) -> Dict:
    """Generate text using DeepSeek with vocabulary words"""
    word_count = WORD_COUNTS[level]
    
    # Create prompt based on level
    if level == 'C2':
        complexity_instruction = """Create a sophisticated, academically rigorous text that provides:
- Critical analysis and nuanced arguments
- Philosophical or theoretical depth
- Multiple perspectives and counterarguments
- Advanced insights beyond surface-level explanations
This is NOT an introductory overview - assume the reader is already familiar with the topic."""
    else:
        complexity_instruction = f"Create an engaging text appropriate for CEFR {level} level."
    
    # Language-specific instructions
    lang_names = {'en': 'English', 'es': 'Spanish', 'zh': 'Chinese'}
    
    prompt = f"""Write a {word_count}-word text in {lang_names[language]} about: {topic}

{complexity_instruction}

Vocabulary words to incorporate naturally (use as many as flow naturally, prioritize natural writing):
{', '.join(words[:30])}

IMPORTANT INSTRUCTIONS:
1. Write naturally and prioritize content quality over forcing vocabulary
2. Use vocabulary words flexibly - adapt tense, form, or use related phrases
3. For phrases like "get things on track", you can use variations like "got their life on track"
4. Focus on the topic and ideas - don't sacrifice coherence to use more words
5. Return your response as JSON with this exact structure:
{{
    "text": "your full text here",
    "words_used": ["word1", "word2", "word3"]
}}

Return ONLY valid JSON, no other text."""

    try:
        response = deepseek_client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": f"Expert {lang_names[language]} content creator. Return valid JSON only."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7,
            timeout=60.0
        )
        
        result_text = response.choices[0].message.content.strip()
        
        # Try to parse JSON
        try:
            result = json.loads(result_text)
        except json.JSONDecodeError:
            # Try to extract JSON if there's extra text
            json_match = re.search(r'\{.*\}', result_text, re.DOTALL)
            if json_match:
                result = json.loads(json_match.group())
            else:
                raise ValueError("Could not parse JSON from response")
        
        return result
        
    except Exception as e:
        logger.error(f"Error generating text: {e}")
        raise


def create_html_with_highlights(text: str, words_used: List[str]) -> str:
    """Create HTML with highlighted vocabulary words"""
    # Strip any markdown bold markers (**word**) before processing
    html_text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    
    # Sort words by length (longest first) to avoid partial matches
    sorted_words = sorted(words_used, key=len, reverse=True)
    
    for word in sorted_words:
        # Use word boundaries and case-insensitive matching
        pattern = re.compile(r'\b' + re.escape(word) + r'\w*\b', re.IGNORECASE)
        html_text = pattern.sub(lambda m: f'<span class="vocab">{m.group()}</span>', html_text)
    
    html_template = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        body {{
            font-family: Georgia, serif;
            line-height: 1.8;
            max-width: 800px;
            margin: 40px auto;
            padding: 20px;
            background-color: #f5f5f5;
        }}
        .content {{
            background-color: white;
            padding: 40px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        .vocab {{
            color: #2563eb;
        }}
        p {{
            margin-bottom: 1.5em;
        }}
    </style>
</head>
<body>
    <div class="content">
        {html_text.replace(chr(10), '</p><p>')}
    </div>
</body>
</html>"""
    
    return html_template


def generate_audio(text: str, language: str, level: str) -> bytes:
    """Generate audio using Google TTS with appropriate speed"""
    # Remove HTML tags and markdown bold markers for audio
    clean_text = re.sub('<[^<]+?>', '', text)
    clean_text = re.sub(r'\*\*(.+?)\*\*', r'\1', clean_text)
    
    # Select random voice
    voice_name = random.choice(CHIRP_VOICES[language])
    
    # Get speed for level
    speed = AUDIO_SPEEDS.get(level, 1.0)
    
    # Map language codes
    lang_codes = {'en': 'en-US', 'es': 'es-ES', 'zh': 'cmn-CN'}
    
    synthesis_input = texttospeech.SynthesisInput(text=clean_text)
    voice = texttospeech.VoiceSelectionParams(
        language_code=lang_codes[language],
        name=voice_name
    )
    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.MP3,
        speaking_rate=speed
    )
    
    response = tts_client.synthesize_speech(
        input=synthesis_input,
        voice=voice,
        audio_config=audio_config
    )
    
    return response.audio_content


@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    bot.reply_to(message, 
        "Welcome! 📚\n\n"
        "Send me:\n"
        "1. A .txt file with Anki cards (tab-delimited)\n"
        "2. Or just paste a column of vocabulary words\n\n"
        "I'll create a custom text using your vocabulary!")


@bot.message_handler(content_types=['document'])
def handle_document(message):
    try:
        file_info = bot.get_file(message.document.file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        
        # Decode text
        text = downloaded_file.decode('utf-8')
        
        # Initialize session
        user_id = message.from_user.id
        user_sessions[user_id] = UserSession()
        user_sessions[user_id].raw_text = text
        user_sessions[user_id].awaiting_column = True
        
        bot.reply_to(message, 
            "Got your file! 📄\n\n"
            "Which column number contains your target vocabulary words?\n"
            "(Enter just the number, e.g., 1 for first column)")
        
    except Exception as e:
        logger.error(f"Error handling document: {e}")
        bot.reply_to(message, "Sorry, I couldn't read that file. Please make sure it's a text file.")


@bot.message_handler(func=lambda message: True)
def handle_message(message):
    user_id = message.from_user.id
    text = message.text.strip()
    
    # Check if user has active session
    if user_id not in user_sessions:
        user_sessions[user_id] = UserSession()
    
    session = user_sessions[user_id]
    
    # Handle column number input
    if session.awaiting_column:
        try:
            column_num = int(text)
            words = parse_column(session.raw_text, column_num)
            
            if not words:
                # Give user diagnostic info
                lines = session.raw_text.lstrip('\ufeff').split('\n')
                first_real = next((l for l in lines if l.strip() and not l.strip().startswith('#')), '')
                tab_count = first_real.count('\t')
                bot.reply_to(message, 
                    f"No words found in column {column_num}.\n\n"
                    f"First data line has {tab_count} tab(s):\n`{first_real[:100]}`\n\n"
                    f"Try column 1, or check if your file uses tabs.",
                    parse_mode="Markdown")
                return
            
            session.words = words
            session.language = detect_language(words)
            session.words = filter_words(words, session.language)
            session.awaiting_column = False
            session.awaiting_confirmation = True
            
            bot.reply_to(message, 
                f"Found {len(session.words)} vocabulary words.\n\n"
                f"Preview: {', '.join(session.words[:10])}{'...' if len(session.words) > 10 else ''}\n\n"
                "Is this correct? (yes/no)")
            
        except ValueError:
            bot.reply_to(message, "Please enter a valid column number (e.g., 1, 2, 3)")
        return
    
    # Handle confirmation
    if session.awaiting_confirmation:
        if text.lower() in ['yes', 'y', 'да', 'sí']:
            session.awaiting_confirmation = False
            
            # Ask for level
            markup = types.ReplyKeyboardMarkup(one_time_keyboard=True, resize_keyboard=True)
            markup.row('C2', 'C1', 'B2')
            markup.row('B1', 'A2', 'A1')
            
            bot.reply_to(message, "Great! Select your language level:", reply_markup=markup)
        else:
            user_sessions.pop(user_id, None)
            bot.reply_to(message, "Okay, let's start over. Send me your vocabulary list.")
        return
    
    # Handle level selection
    if not session.level and text.upper() in WORD_COUNTS:
        session.level = text.upper()
        bot.reply_to(message, 
            f"Perfect! Level {session.level} selected.\n\n"
            "Now, what topic or question would you like the text about?",
            reply_markup=types.ReplyKeyboardRemove())
        return
    
    # Handle topic input
    if session.level and not session.topic:
        session.topic = text
        
        bot.reply_to(message, "Creating your text... ⏳\nThis may take a minute.")
        
        try:
            # Generate text
            result = generate_text_with_vocab(
                session.words,
                session.topic,
                session.level,
                session.language
            )
            
            generated_text = result['text']
            words_used = result['words_used']
            
            # Create HTML
            html_content = create_html_with_highlights(generated_text, words_used)
            
            # Send HTML file
            html_file = io.BytesIO(html_content.encode('utf-8'))
            html_file.name = 'text.html'
            bot.send_document(message.chat.id, html_file, 
                caption=f"✅ Used {len(words_used)} vocabulary words:\n{', '.join(words_used)}")
            
            # Generate and send audio (all levels including C2)
            if session.level != 'NEVER':  # always generate audio
                bot.send_message(message.chat.id, "Generating audio... 🔊")
                audio_content = generate_audio(generated_text, session.language, session.level)
                audio_file = io.BytesIO(audio_content)
                audio_file.name = 'audio.mp3'
                speed_percent = int(AUDIO_SPEEDS[session.level] * 100)
                bot.send_audio(message.chat.id, audio_file, 
                    caption=f"🎧 Audio at {speed_percent}% speed")
            
            # Clear session
            user_sessions.pop(user_id, None)
            bot.send_message(message.chat.id, 
                "Done! 🎉\n\nSend me another vocabulary list when you're ready.")
            
        except Exception as e:
            logger.error(f"Error in generation: {e}")
            bot.reply_to(message, 
                "Sorry, something went wrong. Please try again or contact support.")
            user_sessions.pop(user_id, None)
        
        return
    
    # Handle plain text paste (no file)
    if not session.raw_text and '\n' in text:
        words = [line.strip() for line in text.split('\n') if line.strip()]
        session.words = words
        session.language = detect_language(words)
        session.words = filter_words(words, session.language)
        session.awaiting_confirmation = True
        
        bot.reply_to(message, 
            f"Found {len(session.words)} vocabulary words.\n\n"
            f"Preview: {', '.join(session.words[:10])}{'...' if len(session.words) > 10 else ''}\n\n"
            "Is this your vocabulary list? (yes/no)")
        return
    
    # Default response
    bot.reply_to(message, 
        "Please send me a .txt file or paste your vocabulary words (one per line).")


if __name__ == '__main__':
    logger.info("Bot starting...")
    bot.infinity_polling()