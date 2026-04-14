#!/usr/bin/env python3
"""
Feed Me Chinese Bot - Enhanced Version
Two modes:
1. VOCAB MODE - Paste vocabulary list → generates texts (original functionality)
2. PODCAST MODE - Topic input → generates HSK 4 dialogue + phrases + audio (new feature)

Commands:
/start - Show menu
/vocab - Vocabulary text generation mode
/podcast - Podcast dialogue generation mode
"""

import os
import json
import re
import zipfile
import tempfile
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Tuple, Optional
from io import BytesIO

import telebot
from openai import OpenAI
from google.cloud import texttospeech
from google.oauth2 import service_account
import google.auth.transport.requests
from tenacity import retry, stop_after_attempt, wait_exponential

# ============================================================================
# Configuration
# ============================================================================

class Config:
    # API Keys
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
    GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")
    
    # Load Google credentials from file if not in env
    if not GOOGLE_CREDENTIALS_JSON and os.path.exists("google-creds.json"):
        with open("google-creds.json", "r") as f:
            GOOGLE_CREDENTIALS_JSON = f.read()
    
    # Models
    DEEPSEEK_MODEL = "deepseek-chat"
    GOOGLE_PROJECT = "anki-332914"
    VERTEX_LOCATION = "us-central1"
    
    # TTS
    CHIRP3_VOICES = {
        "male": "cmn-CN-Chirp3-HD-A",
        "female": "cmn-CN-Chirp3-HD-B"
    }
    SPEAKING_RATE = 0.85  # 85% speed
    
    # Limits
    MAX_VOCAB_WORDS = 20
    MAX_PODCAST_LINES = 12
    MAX_PHRASES = 10


config = Config()

# Validate tokens
if not config.TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN env var not set")
if not config.DEEPSEEK_API_KEY:
    raise RuntimeError("DEEPSEEK_API_KEY env var not set")
if not config.GOOGLE_CREDENTIALS_JSON:
    raise RuntimeError("GOOGLE_CREDENTIALS_JSON env var not set or google-creds.json not found")

bot = telebot.TeleBot(config.TELEGRAM_BOT_TOKEN)

# User session storage
user_sessions = {}

# ============================================================================
# Clients
# ============================================================================

def get_deepseek_client() -> OpenAI:
    return OpenAI(api_key=config.DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")

def get_google_tts_client():
    credentials_dict = json.loads(config.GOOGLE_CREDENTIALS_JSON)
    credentials = service_account.Credentials.from_service_account_info(
        credentials_dict,
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    return texttospeech.TextToSpeechClient(credentials=credentials)

# ============================================================================
# MODE 1: VOCABULARY TEXT GENERATION (Original)
# ============================================================================

def parse_vocabulary_list(text: str) -> List[str]:
    """Parse vocabulary list (space or tab separated)."""
    lines = text.strip().split('\n')
    vocab_items = []
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
        
        # Remove leading numbers
        line = re.sub(r'^\d+\.?\s*', '', line)
        
        # Split by tab or multiple spaces
        parts = re.split(r'\t+|\s{2,}', line)
        
        if len(parts) >= 1:
            word = parts[0].strip()
            if word:
                vocab_items.append(word)
    
    return vocab_items[:config.MAX_VOCAB_WORDS]


@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=1, max=5))
def generate_vocab_text(topic: str, vocab: List[str], deepseek_client: OpenAI) -> str:
    """Generate reading text using vocabulary (original mode)."""
    
    prompt = f"""You are a Chinese language teacher creating HSK 4 level reading material.

Topic: {topic}
Vocabulary to use (must use ALL these words): {', '.join(vocab)}

Create a short, engaging text (150-200 characters) that:
1. Is HSK 4 level (simple, clear language)
2. Uses all the vocabulary provided
3. Is about the topic
4. Is natural and interesting to read

Return ONLY the Chinese text, no translation or explanation."""

    response = deepseek_client.chat.completions.create(
        model=config.DEEPSEEK_MODEL,
        messages=[
            {"role": "system", "content": "You are a HSK 4 Chinese language teacher."},
            {"role": "user", "content": prompt}
        ],
        temperature=0.7,
        timeout=15
    )
    
    return response.choices[0].message.content.strip()


@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=1, max=5))
def translate_text(text_zh: str, deepseek_client: OpenAI) -> str:
    """Translate Chinese text to English."""
    
    response = deepseek_client.chat.completions.create(
        model=config.DEEPSEEK_MODEL,
        messages=[
            {"role": "user", "content": f"Translate to English:\n{text_zh}\n\nRespond with ONLY the English translation."}
        ],
        temperature=0.3,
        timeout=10
    )
    
    return response.choices[0].message.content.strip()


def generate_chirp3_tts(text: str, gender: str, tts_client) -> Tuple[bytes, bool]:
    """Generate Chirp3 TTS audio."""
    try:
        voice_name = config.CHIRP3_VOICES.get(gender, config.CHIRP3_VOICES["male"])
        
        synthesis_input = texttospeech.SynthesisInput(text=text)
        voice = texttospeech.VoiceSelectionParams(language_code="cmn-CN", name=voice_name)
        audio_config = texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.MP3,
            speaking_rate=config.SPEAKING_RATE
        )
        
        response = tts_client.synthesize_speech(
            input=synthesis_input,
            voice=voice,
            audio_config=audio_config,
            timeout=30
        )
        
        return response.audio_content if response.audio_content else None, bool(response.audio_content)
    except Exception as e:
        print(f"[TTS Error] {e}")
        return None, False


# ============================================================================
# MODE 2: PODCAST DIALOGUE GENERATION (New)
# ============================================================================

@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=1, max=5))
def generate_podcast_dialogue(
    topic: str,
    deepseek_client: OpenAI,
    num_exchanges: int = 6
) -> List[Dict[str, str]]:
    """Generate HSK 4 dialogue for podcast."""
    
    prompt = f"""You are a Mandarin Chinese dialogue writer for HSK 4 learners.

Create a conversation about: "{topic}"

Rules:
1. Two speakers: 张老师 (Teacher Zhang) and 小王 (Xiao Wang)
2. Total lines: {num_exchanges * 2}
3. Each line: 15-35 characters max
4. HSK 4 level (simple vocabulary)
5. Natural, conversational tone

Format as JSON (ONLY JSON, no markdown):
[
  {{"speaker": "张老师", "text_zh": "你好...", "text_en": "Hello..."}}
]"""

    response = deepseek_client.chat.completions.create(
        model=config.DEEPSEEK_MODEL,
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


@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=1, max=5))
def extract_podcast_phrases(
    dialogue: List[Dict[str, str]],
    deepseek_client: OpenAI
) -> List[Dict[str, str]]:
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

    response = deepseek_client.chat.completions.create(
        model=config.DEEPSEEK_MODEL,
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


# ============================================================================
# Bot Commands & Handlers
# ============================================================================

@bot.message_handler(commands=['start'])
def start_command(message):
    """Show main menu."""
    chat_id = message.chat.id
    
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(
        telebot.types.KeyboardButton("📚 Vocabulary Mode"),
        telebot.types.KeyboardButton("🎙️ Podcast Mode")
    )
    
    bot.send_message(
        chat_id,
        "👋 Welcome to Feed Me Chinese!\n\n"
        "Choose a mode:\n\n"
        "📚 **Vocabulary Mode** — Paste words → generates texts\n"
        "🎙️ **Podcast Mode** — Topic → generates HSK 4 dialogue + audio\n\n"
        "or use commands:\n"
        "/vocab - Vocabulary text generation\n"
        "/podcast - Podcast dialogue generation",
        reply_markup=markup,
        parse_mode="Markdown"
    )


@bot.message_handler(commands=['vocab'])
def vocab_command(message):
    """Start vocabulary mode."""
    chat_id = message.chat.id
    msg = bot.send_message(
        chat_id,
        "📚 **Vocabulary Mode**\n\n"
        "Paste a list of Chinese words (one per line or tab-separated).\n\n"
        "Example:\n"
        "```\n"
        "影响 affect\n"
        "经济 economy\n"
        "战争 war\n"
        "```\n\n"
        "I'll generate a short HSK 4 text using these words.",
        parse_mode="Markdown"
    )
    user_sessions[chat_id] = {'mode': 'vocab_input'}
    bot.register_next_step_handler(msg, vocab_input_handler)


@bot.message_handler(commands=['podcast'])
def podcast_command(message):
    """Start podcast mode."""
    chat_id = message.chat.id
    msg = bot.send_message(
        chat_id,
        "🎙️ **Podcast Mode**\n\n"
        "Enter a topic for HSK 4 podcast dialogue.\n\n"
        "Examples:\n"
        "• Iran war and global economy\n"
        "• Climate change\n"
        "• Technology and AI\n"
        "• Travel and culture\n\n"
        "I'll generate:\n"
        "✓ HSK 4 dialogue (6 exchanges)\n"
        "✓ 10 useful phrases for Anki\n"
        "✓ Chinese + English text\n"
        "✓ Audio files (Chirp3 at 85% speed)",
        parse_mode="Markdown"
    )
    user_sessions[chat_id] = {'mode': 'podcast_input'}
    bot.register_next_step_handler(msg, podcast_input_handler)


def vocab_input_handler(message):
    """Handle vocabulary list input."""
    chat_id = message.chat.id
    
    if message.text.startswith('/'):
        start_command(message)
        return
    
    # Parse vocabulary
    vocab = parse_vocabulary_list(message.text)
    
    if not vocab:
        msg = bot.send_message(chat_id, "❌ No valid words found. Please try again with format:\nword1\nword2\nword3")
        bot.register_next_step_handler(msg, vocab_input_handler)
        return
    
    # Ask for topic
    msg = bot.send_message(
        chat_id,
        f"✓ Found {len(vocab)} words:\n{', '.join(vocab)}\n\n"
        f"Now, what topic should the text be about?\n"
        f"(e.g., 'Iran war', 'climate change', 'technology')"
    )
    user_sessions[chat_id] = {'mode': 'vocab_topic', 'vocab': vocab}
    bot.register_next_step_handler(msg, vocab_topic_handler)


def vocab_topic_handler(message):
    """Handle topic input for vocabulary mode."""
    chat_id = message.chat.id
    session = user_sessions.get(chat_id, {})
    vocab = session.get('vocab', [])
    topic = message.text.strip()
    
    if not topic:
        msg = bot.send_message(chat_id, "❌ Topic cannot be empty. Please try again.")
        bot.register_next_step_handler(msg, vocab_topic_handler)
        return
    
    # Generate text
    status = bot.send_message(chat_id, "⏳ Generating text using vocabulary...\n(This takes ~10 seconds)")
    
    try:
        deepseek_client = get_deepseek_client()
        text_zh = generate_vocab_text(topic, vocab, deepseek_client)
        
        bot.edit_message_text(
            "⏳ Translating to English...",
            chat_id,
            status.message_id
        )
        
        text_en = translate_text(text_zh, deepseek_client)
        
        # Generate TTS
        bot.edit_message_text(
            "⏳ Generating audio (85% speed)...",
            chat_id,
            status.message_id
        )
        
        tts_client = get_google_tts_client()
        audio_data, success = generate_chirp3_tts(text_zh, "female", tts_client)
        
        # Send results
        bot.delete_message(chat_id, status.message_id)
        
        result_text = (
            f"📚 **Vocabulary Text**\n\n"
            f"**Topic:** {topic}\n"
            f"**Words:** {', '.join(vocab)}\n\n"
            f"🇨🇳 {text_zh}\n\n"
            f"🇬🇧 {text_en}"
        )
        
        bot.send_message(chat_id, result_text, parse_mode="Markdown")
        
        if success and audio_data:
            audio_buffer = BytesIO(audio_data)
            audio_buffer.name = "vocab_text.mp3"
            bot.send_audio(chat_id, audio_buffer, caption="🔊 Pronunciation (85% speed)")
        
        # Show menu again
        show_menu(chat_id)
        
    except Exception as e:
        bot.edit_message_text(f"❌ Error: {e}", chat_id, status.message_id)
        show_menu(chat_id)


def podcast_input_handler(message):
    """Handle topic input for podcast mode."""
    chat_id = message.chat.id
    
    if message.text.startswith('/'):
        start_command(message)
        return
    
    topic = message.text.strip()
    
    if not topic or len(topic) < 3:
        msg = bot.send_message(chat_id, "❌ Topic must be at least 3 characters. Please try again.")
        user_sessions[chat_id] = {'mode': 'podcast_input'}
        bot.register_next_step_handler(msg, podcast_input_handler)
        return
    
    # Generate podcast
    status = bot.send_message(
        chat_id,
        f"🎙️ Creating podcast: **{topic}**\n\n"
        f"⏳ Generating HSK 4 dialogue...\n"
        f"(This takes ~30-60 seconds)",
        parse_mode="Markdown"
    )
    
    try:
        deepseek_client = get_deepseek_client()
        tts_client = get_google_tts_client()
        
        # Generate dialogue
        dialogue = generate_podcast_dialogue(topic, deepseek_client, num_exchanges=6)
        
        if not dialogue:
            raise Exception("Failed to generate dialogue")
        
        bot.edit_message_text(
            f"🎙️ Creating podcast: **{topic}**\n\n"
            f"✓ Generated {len(dialogue)} lines\n"
            f"⏳ Extracting phrases...",
            chat_id,
            status.message_id,
            parse_mode="Markdown"
        )
        
        # Extract phrases
        phrases = extract_podcast_phrases(dialogue, deepseek_client)
        
        bot.edit_message_text(
            f"🎙️ Creating podcast: **{topic}**\n\n"
            f"✓ Generated {len(dialogue)} lines\n"
            f"✓ Extracted {len(phrases)} phrases\n"
            f"⏳ Generating audio files...",
            chat_id,
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
                
                # Generate TTS
                audio_data, success = generate_chirp3_tts(text_zh, gender, tts_client)
                
                if audio_data:
                    safe_speaker = re.sub(r'[^\w]', '_', speaker)
                    filename = f"{i:02d}_{safe_speaker}.mp3"
                    zf.writestr(filename, audio_data)
        
        audio_zip_data.seek(0)
        
        bot.delete_message(chat_id, status.message_id)
        
        # Send dialogue text
        dialogue_text = f"🎙️ **{topic}** (HSK 4 Podcast)\n\n"
        for i, line in enumerate(dialogue, 1):
            speaker = line.get("speaker", "?")
            text_zh = line.get("text_zh", "")
            text_en = line.get("text_en", "")
            dialogue_text += f"{i}. {speaker}\n🇨🇳 {text_zh}\n🇬🇧 {text_en}\n\n"
        
        bot.send_message(chat_id, dialogue_text, parse_mode="Markdown")
        
        # Send phrases
        phrases_text = "📚 **Useful Phrases** (for Anki)\n\n"
        for i, phrase in enumerate(phrases, 1):
            phrase_zh = phrase.get("phrase_zh", "")
            phrase_en = phrase.get("phrase_en", "")
            phrases_text += f"{i}. {phrase_zh} — {phrase_en}\n"
        
        bot.send_message(chat_id, phrases_text, parse_mode="Markdown")
        
        # Send audio ZIP
        audio_zip_data.name = "podcast_audio.zip"
        bot.send_document(
            chat_id,
            audio_zip_data,
            caption="🎵 Podcast Audio Files (85% speed)\nExtract and play in any audio player"
        )
        
        # Show menu again
        show_menu(chat_id)
        
    except Exception as e:
        print(f"Error: {e}")
        bot.edit_message_text(f"❌ Error: {str(e)[:100]}", chat_id, status.message_id)
        show_menu(chat_id)


def show_menu(chat_id):
    """Show main menu."""
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(
        telebot.types.KeyboardButton("📚 Vocabulary Mode"),
        telebot.types.KeyboardButton("🎙️ Podcast Mode")
    )
    
    bot.send_message(
        chat_id,
        "Choose another mode or use /vocab or /podcast",
        reply_markup=markup
    )


@bot.message_handler(func=lambda message: message.text == "📚 Vocabulary Mode")
def vocab_button_handler(message):
    vocab_command(message)


@bot.message_handler(func=lambda message: message.text == "🎙️ Podcast Mode")
def podcast_button_handler(message):
    podcast_command(message)


@bot.message_handler(func=lambda message: True)
def unknown_handler(message):
    bot.reply_to(message, "Use /start to see menu or /vocab or /podcast")


# ============================================================================
# Main
# ============================================================================

def main():
    print("🤖 Feed Me Chinese Bot - Enhanced (Vocab + Podcast)")
    print("🚀 Starting bot...")
    
    bot.infinity_polling(allowed_updates=['message', 'callback_query'])


if __name__ == "__main__":
    main()
