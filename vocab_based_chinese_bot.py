import os
import json
import hashlib
import re
import zipfile
import time
from datetime import datetime
from collections import defaultdict
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from openai import OpenAI
from google.cloud import texttospeech
from google.oauth2 import service_account
import asyncio
from io import BytesIO
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# Environment variables
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")

# Load Google credentials from file if not in env
if not GOOGLE_CREDENTIALS_JSON and os.path.exists("google-creds.json"):
    with open("google-creds.json", "r") as f:
        GOOGLE_CREDENTIALS_JSON = f.read()

# Configuration
class Config:
    MAX_TOPIC_LENGTH = 200
    TTS_TIMEOUT = 30
    API_RETRY_ATTEMPTS = 3
    RATE_LIMIT_REQUESTS = 5
    RATE_LIMIT_WINDOW = 3600
    MAX_FILE_SIZE = 50 * 1024 * 1024

    # Gemini 2.5 Pro TTS voices (via Vertex AI)
    GEMINI_TTS_VOICES = ["Aoede", "Leda", "Puck", "Kore", "Charon"]
    ANKI_VOICE = "Kore"

    # Google Cloud project for Vertex AI
    GOOGLE_PROJECT = "anki-332914"
    VERTEX_LOCATION = "us-central1"
    GEMINI_TTS_MODEL = "gemini-2.5-pro-preview-tts"

    # Chirp3 fallback voices (used if Gemini fails)
    CHIRP3_FALLBACK_VOICES = [
        "cmn-CN-Chirp3-HD-Aoede",
        "cmn-CN-Chirp3-HD-Leda",
        "cmn-CN-Chirp3-HD-Puck"
    ]
    WAVENET_FALLBACK_VOICES = [
        "cmn-CN-Wavenet-A",
        "cmn-CN-Wavenet-B"
    ]

config = Config()

deepseek_client = OpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url="https://api.deepseek.com"
)

# User session storage
user_sessions = {}

class RateLimiter:
    def __init__(self, max_requests=5, window=3600):
        self.requests = defaultdict(list)
        self.max_requests = max_requests
        self.window = window
    
    def is_allowed(self, user_id):
        now = time.time()
        user_requests = self.requests[user_id]
        user_requests[:] = [req_time for req_time in user_requests if now - req_time < self.window]
        
        if len(user_requests) >= self.max_requests:
            return False
        
        user_requests.append(now)
        return True
    
    def get_reset_time(self, user_id):
        if not self.requests[user_id]:
            return 0
        oldest_request = min(self.requests[user_id])
        reset_time = oldest_request + self.window - time.time()
        return max(0, int(reset_time))

rate_limiter = RateLimiter(
    max_requests=config.RATE_LIMIT_REQUESTS,
    window=config.RATE_LIMIT_WINDOW
)

def get_google_tts_client():
    if GOOGLE_CREDENTIALS_JSON:
        credentials_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
        credentials = service_account.Credentials.from_service_account_info(
            credentials_dict,
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        return texttospeech.TextToSpeechClient(credentials=credentials)
    else:
        return texttospeech.TextToSpeechClient()

# ============================================================================
# MODE 1: VOCABULARY TEXT (ORIGINAL)
# ============================================================================

def parse_vocabulary_list(text):
    """Parse English-Russian vocabulary list, extract only English"""
    lines = text.strip().split('\n')
    vocab_items = []
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
        
        # Remove leading numbers (e.g., "1", "2.", etc.)
        line = re.sub(r'^\d+\.?\s*', '', line)
        
        # Split by tab or multiple spaces
        parts = re.split(r'\t+|\s{2,}', line)
        
        if len(parts) >= 2:
            english = parts[0].strip()
            # Ignore Russian (second part)
            if english:
                vocab_items.append(english)
    
    return vocab_items

# ============================================================================
# MODE 2: PODCAST DIALOGUE FROM NOTES (NEW)
# ============================================================================

@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=1, max=5))
def generate_podcast_dialogue(topic: str, notes: str, num_exchanges: int = 6):
    """Generate HSK 4 level dialogue based on notes using DeepSeek."""
    
    # Truncate notes if too long
    notes_truncated = notes[:1500] if len(notes) > 1500 else notes
    
    prompt = f"""You are a Mandarin Chinese dialogue writer for HSK 4 learners.

Topic: "{topic}"

Source material (notes/article):
{notes_truncated}

Create a conversation about this topic based on the notes provided.

Rules:
1. Two speakers: 张老师 (Teacher Zhang) and 小王 (Xiao Wang)
2. Total lines: {num_exchanges * 2}
3. Each line: 15-35 characters max
4. HSK 4 level (simple vocabulary)
5. Natural, conversational tone
6. Content should be based on the provided notes/article
7. Include key facts and vocabulary from the notes

Format as JSON (ONLY JSON, no markdown):
[
  {{"speaker": "张老师", "text_zh": "你好...", "text_en": "Hello..."}}
]"""

    try:
        response = deepseek_client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": "You are a HSK 4 dialogue writer."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7,
            timeout=20
        )
        
        content = response.choices[0].message.content.strip()
        
        # Extract JSON
        json_match = re.search(r'\[.*\]', content, re.DOTALL)
        if json_match:
            return json.loads(json_match.group())
        
        return []
    except Exception as e:
        print(f"[Podcast error] {e}")
        return []


@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=1, max=5))
def extract_podcast_phrases(dialogue: list):
    """Extract 10 useful phrases from dialogue."""
    
    all_text = " ".join([line.get("text_zh", "") for line in dialogue])
    
    prompt = f"""Extract 10 useful HSK 4 phrases (2-6 characters each) from this dialogue:

"{all_text}"

Rules:
1. 2-6 characters per phrase
2. Must appear in the text
3. Useful for learners
4. Return as JSON only (no markdown)

Format:
[
  {{"phrase_zh": "影响", "phrase_en": "affect"}}
]"""

    try:
        response = deepseek_client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "user", "content": prompt}
            ],
            temperature=0.5,
            timeout=15
        )
        
        content = response.choices[0].message.content.strip()
        json_match = re.search(r'\[.*\]', content, re.DOTALL)
        
        if json_match:
            return json.loads(json_match.group())
        
        return []
    except Exception as e:
        print(f"[Phrase extraction error] {e}")
        return []


def _chirp3_tts(text: str, voice_name: str = "cmn-CN-Chirp3-HD-Aoede", speaking_rate: float = 0.85):
    """Generate Chirp3 HD TTS."""
    try:
        client = get_google_tts_client()
        synthesis_input = texttospeech.SynthesisInput(text=text)
        voice = texttospeech.VoiceSelectionParams(
            language_code="cmn-CN",
            name=voice_name
        )
        audio_config = texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.MP3,
            speaking_rate=max(0.25, min(2.0, speaking_rate))
        )
        response = client.synthesize_speech(
            input=synthesis_input,
            voice=voice,
            audio_config=audio_config,
            timeout=config.TTS_TIMEOUT
        )
        if response.audio_content:
            return response.audio_content, True
        return None, False
    except Exception as e:
        print(f"[Chirp3 TTS error] {e}")
        return None, False


# ============================================================================
# BOT COMMANDS
# ============================================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show main menu."""
    await update.message.reply_text(
        "👋 Welcome to Feed Me Chinese!\n\n"
        "Choose a mode:\n\n"
        "📚 /vocab — Paste words → generates HSK 4 text\n"
        "🎙️ /podcast — Enter topic + notes → generates HSK 4 dialogue + audio\n"
    )


async def vocab_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start vocabulary mode."""
    await update.message.reply_text(
        "📚 **Vocabulary Mode**\n\n"
        "Paste a list of Chinese words (one per line or tab-separated).\n\n"
        "Example:\n"
        "影响 affect\n"
        "经济 economy\n"
        "战争 war\n\n"
        "I'll generate a short HSK 4 text using these words.",
        parse_mode="Markdown"
    )
    context.user_data['mode'] = 'vocab_input'


async def podcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start podcast mode - step 1: get topic."""
    await update.message.reply_text(
        "🎙️ **Podcast Mode**\n\n"
        "Step 1: Enter a topic for your podcast.\n\n"
        "Examples:\n"
        "• Iran war and global economy\n"
        "• Climate change\n"
        "• Technology and AI\n\n"
        "(Then you'll paste your notes/article)",
        parse_mode="Markdown"
    )
    context.user_data['mode'] = 'podcast_topic'


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text messages based on mode."""
    mode = context.user_data.get('mode', None)
    
    if mode == 'vocab_input':
        await handle_vocab_input(update, context)
    elif mode == 'vocab_topic':
        await handle_vocab_topic(update, context)
    elif mode == 'podcast_topic':
        await handle_podcast_topic(update, context)
    elif mode == 'podcast_notes':
        await handle_podcast_notes(update, context)
    else:
        await update.message.reply_text("Use /vocab or /podcast to start")


async def handle_vocab_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle vocabulary list input."""
    text = update.message.text
    
    # Parse vocabulary
    vocab = parse_vocabulary_list(text)
    
    if not vocab:
        await update.message.reply_text("❌ No valid words found. Try again with format:\nword1\nword2\nword3")
        return
    
    # Ask for topic
    await update.message.reply_text(
        f"✓ Found {len(vocab)} words:\n{', '.join(vocab[:5])}{'...' if len(vocab) > 5 else ''}\n\n"
        f"Now, what topic should the text be about?\n"
        f"(e.g., 'Iran war', 'climate change')"
    )
    context.user_data['vocab'] = vocab
    context.user_data['mode'] = 'vocab_topic'


async def handle_vocab_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle topic input for vocabulary mode."""
    topic = update.message.text.strip()
    vocab = context.user_data.get('vocab', [])
    
    if not topic:
        await update.message.reply_text("❌ Topic cannot be empty.")
        return
    
    status = await update.message.reply_text("⏳ Generating text and audio...")
    
    try:
        # Original vocab text generation (from your old bot)
        # This would go here - for now, just send placeholder
        text_zh = f"关于{topic}的文本。"
        text_en = f"Text about {topic}."
        
        # Generate audio
        audio_data, success = _chirp3_tts(text_zh, "cmn-CN-Chirp3-HD-B", 0.85)
        
        result_text = f"📚 **{topic}**\n\n🇨🇳 {text_zh}\n🇬🇧 {text_en}"
        await context.bot.edit_message_text(result_text, update.effective_chat.id, status.message_id, parse_mode="Markdown")
        
        if success and audio_data:
            await context.bot.send_audio(update.effective_chat.id, audio_data, caption="🔊 Audio (85% speed)")
        
        # Reset
        context.user_data['mode'] = None
        await update.message.reply_text("Choose another mode:\n/vocab or /podcast")
        
    except Exception as e:
        await context.bot.edit_message_text(f"❌ Error: {str(e)[:100]}", update.effective_chat.id, status.message_id)


async def handle_podcast_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle topic input for podcast - step 1."""
    topic = update.message.text.strip()
    
    if not topic or len(topic) < 3:
        await update.message.reply_text("❌ Topic must be at least 3 characters.")
        return
    
    # Store topic and ask for notes
    context.user_data['podcast_topic'] = topic
    context.user_data['mode'] = 'podcast_notes'
    
    await update.message.reply_text(
        f"✓ Topic: **{topic}**\n\n"
        f"Step 2: Paste your notes/article about this topic.\n\n"
        f"(The longer and more detailed, the better the dialogue!)",
        parse_mode="Markdown"
    )


async def handle_podcast_notes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle notes input for podcast - step 2."""
    notes = update.message.text.strip()
    topic = context.user_data.get('podcast_topic', 'Unknown')
    
    if not notes or len(notes) < 10:
        await update.message.reply_text("❌ Notes must be at least 10 characters. Please provide more detail.")
        return
    
    status = await update.message.reply_text(
        f"🎙️ Creating podcast: **{topic}**\n⏳ Generating dialogue from notes...",
        parse_mode="Markdown"
    )
    
    try:
        # Generate dialogue based on notes
        dialogue = generate_podcast_dialogue(topic, notes, num_exchanges=6)
        
        if not dialogue:
            raise Exception("Failed to generate dialogue")
        
        await context.bot.edit_message_text(
            f"🎙️ Creating podcast: **{topic}**\n✓ Generated {len(dialogue)} lines\n⏳ Extracting phrases...",
            update.effective_chat.id,
            status.message_id,
            parse_mode="Markdown"
        )
        
        # Extract phrases
        phrases = extract_podcast_phrases(dialogue)
        
        await context.bot.edit_message_text(
            f"🎙️ Creating podcast: **{topic}**\n✓ Generated {len(dialogue)} lines\n✓ Extracted {len(phrases)} phrases\n⏳ Generating audio...",
            update.effective_chat.id,
            status.message_id,
            parse_mode="Markdown"
        )
        
        # Generate audio and create ZIP
        audio_zip_data = BytesIO()
        with zipfile.ZipFile(audio_zip_data, 'w', zipfile.ZIP_DEFLATED) as zf:
            for i, line in enumerate(dialogue, 1):
                speaker = line.get("speaker", "?")
                text_zh = line.get("text_zh", "")
                
                # Determine gender
                gender = "male" if "张" in speaker or "老师" in speaker else "female"
                voice = "cmn-CN-Chirp3-HD-A" if gender == "male" else "cmn-CN-Chirp3-HD-B"
                
                # Generate TTS
                audio_data, success = _chirp3_tts(text_zh, voice, 0.85)
                
                if audio_data:
                    safe_speaker = re.sub(r'[^\w]', '_', speaker)
                    filename = f"{i:02d}_{safe_speaker}.mp3"
                    zf.writestr(filename, audio_data)
        
        audio_zip_data.seek(0)
        
        await context.bot.delete_message(update.effective_chat.id, status.message_id)
        
        # Send dialogue
        dialogue_text = f"🎙️ **{topic}** (HSK 4)\n\n"
        for i, line in enumerate(dialogue, 1):
            speaker = line.get("speaker", "?")
            text_zh = line.get("text_zh", "")
            text_en = line.get("text_en", "")
            dialogue_text += f"{i}. {speaker}\n🇨🇳 {text_zh}\n🇬🇧 {text_en}\n\n"
        
        await update.message.reply_text(dialogue_text, parse_mode="Markdown")
        
        # Send phrases
        phrases_text = "📚 **Phrases for Anki**\n\n"
        for i, phrase in enumerate(phrases, 1):
            phrase_zh = phrase.get("phrase_zh", "")
            phrase_en = phrase.get("phrase_en", "")
            phrases_text += f"{i}. {phrase_zh} — {phrase_en}\n"
        
        await update.message.reply_text(phrases_text, parse_mode="Markdown")
        
        # Send audio ZIP
        audio_zip_data.name = "podcast_audio.zip"
        await context.bot.send_document(
            update.effective_chat.id,
            audio_zip_data,
            caption="🎵 Audio Files (85% speed)"
        )
        
        context.user_data['mode'] = None
        await update.message.reply_text("Choose another mode:\n/vocab or /podcast")
        
    except Exception as e:
        print(f"Error: {e}")
        await context.bot.edit_message_text(f"❌ Error: {str(e)[:100]}", update.effective_chat.id, status.message_id)
        context.user_data['mode'] = None


# ============================================================================
# MAIN
# ============================================================================

def main():
    """Start the bot"""
    if not TELEGRAM_BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN not set")
        return
    
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    # Commands
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("vocab", vocab_command))
    app.add_handler(CommandHandler("podcast", podcast_command))
    
    # Messages (must be before catch-all)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    print("🤖 Feed Me Chinese Bot Enhanced - Starting...")
    app.run_polling(allowed_updates=['message', 'callback_query'])


if __name__ == "__main__":
    main()
