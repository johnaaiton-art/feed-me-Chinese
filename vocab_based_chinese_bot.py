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

def validate_topic(topic):
    topic = re.sub(r'\s+', ' ', topic.strip())
    
    if re.search(r'[<>"|&;`$()]', topic):
        raise ValueError("Topic contains invalid characters")
    
    if len(topic) > config.MAX_TOPIC_LENGTH:
        topic = topic[:config.MAX_TOPIC_LENGTH]
    
    if not topic:
        raise ValueError("Topic cannot be empty")
    
    return topic

def _get_vertex_token():
    """Get a fresh OAuth2 access token from the service account credentials."""
    import google.auth.transport.requests
    credentials_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
    credentials = service_account.Credentials.from_service_account_info(
        credentials_dict,
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    credentials.refresh(google.auth.transport.requests.Request())
    return credentials.token

def _chirp3_fallback(text, speaking_rate=1.0):
    """Chirp3 HD via Cloud TTS — used when Gemini TTS fails."""
    try:
        client = get_google_tts_client()
        import random
        voice_name = random.choice(config.CHIRP3_FALLBACK_VOICES)
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
            print(f"[TTS Chirp3 fallback] ✅ {voice_name}")
            return response.audio_content, voice_name, True
        return None, None, False
    except Exception as e:
        print(f"[TTS Chirp3 fallback] ❌ {e}")
        # Last resort: Wavenet
        try:
            client = get_google_tts_client()
            voice_name = config.WAVENET_FALLBACK_VOICES[0]
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
            return response.audio_content, voice_name, bool(response.audio_content)
        except Exception as e2:
            print(f"[TTS Wavenet fallback] ❌ {e2}")
            return None, None, False

def generate_tts_chirp3_sync(text, voice_name=None, speaking_rate=1.0):
    """
    Generate TTS using Gemini 2.5 Pro TTS via Vertex AI REST API.
    Falls back to Chirp3 HD automatically if Gemini fails.
    Returns (audio_bytes_mp3, voice_used, success).
    """
    import random
    import base64
    import urllib.request
    import subprocess
    import tempfile

    voice = voice_name if voice_name and voice_name in config.GEMINI_TTS_VOICES \
        else random.choice(config.GEMINI_TTS_VOICES)

    pace = "a slower" if speaking_rate < 1.0 else "a normal"
    style_prompt = f"Read aloud naturally in Mandarin Chinese at {pace} pace.\n\n{text}"

    payload = json.dumps({
        "contents": [{"role": "user", "parts": [{"text": style_prompt}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "voiceConfig": {
                    "prebuiltVoiceConfig": {"voiceName": voice}
                }
            }
        }
    }).encode("utf-8")

    url = (
        f"https://{config.VERTEX_LOCATION}-aiplatform.googleapis.com/v1/projects/"
        f"{config.GOOGLE_PROJECT}/locations/{config.VERTEX_LOCATION}/"
        f"publishers/google/models/{config.GEMINI_TTS_MODEL}:generateContent"
    )

    try:
        token = _get_vertex_token()
        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            }
        )
        print(f"[TTS Gemini] Trying voice: {voice} for '{text[:30]}...' @ {speaking_rate}x")
        with urllib.request.urlopen(req, timeout=config.TTS_TIMEOUT) as resp:
            result = json.loads(resp.read().decode("utf-8"))

        # Extract base64-encoded PCM audio
        audio_b64 = (
            result["candidates"][0]["content"]["parts"][0]
            ["inlineData"]["data"]
        )
        pcm_bytes = base64.b64decode(audio_b64)

        # Convert raw PCM (s16le, 24kHz, mono) to MP3 via ffmpeg
        with tempfile.NamedTemporaryFile(suffix=".pcm", delete=False) as pcm_file:
            pcm_file.write(pcm_bytes)
            pcm_path = pcm_file.name
        mp3_path = pcm_path.replace(".pcm", ".mp3")
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-f", "s16le", "-ar", "24000", "-ac", "1",
                "-i", pcm_path,
                mp3_path
            ],
            check=True,
            capture_output=True
        )
        with open(mp3_path, "rb") as f:
            mp3_bytes = f.read()
        os.remove(pcm_path)
        os.remove(mp3_path)

        print(f"[TTS Gemini] ✅ {voice} ({len(mp3_bytes)} bytes)")
        return mp3_bytes, voice, True

    except Exception as e:
        print(f"[TTS Gemini] ❌ {voice} failed: {type(e).__name__}: {e}")
        print("[TTS Gemini] Falling back to Chirp3...")
        return _chirp3_fallback(text, speaking_rate)

async def generate_tts_async(text, voice_name=None, speaking_rate=1.0):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, generate_tts_chirp3_sync, text, voice_name, speaking_rate)

def safe_filename(filename):
    filename = re.sub(r'[^\w\s.-]', '', filename)
    filename = filename.replace('..', '').replace('/', '').replace('\\', '')
    filename = os.path.basename(filename)
    filename = filename[:100]
    return filename.strip('_')

@retry(
    stop=stop_after_attempt(config.API_RETRY_ATTEMPTS),
    wait=wait_exponential(multiplier=1, min=4, max=10),
    retry=retry_if_exception_type((Exception,)),
    before_sleep=lambda retry_state: print(f"Retry {retry_state.attempt_number}: {retry_state.outcome.exception()}")
)
def translate_vocabulary_to_chinese(english_phrases):
    """Translate English phrases to Chinese with pinyin"""
    print(f"[DeepSeek] Translating {len(english_phrases)} phrases to Chinese...")
    
    phrases_list = "\n".join([f"{i+1}. {phrase}" for i, phrase in enumerate(english_phrases)])
    
    prompt = f"""Translate these English phrases to Mandarin Chinese (Simplified).
For each phrase, provide:
- Chinese translation (natural, HSK5 level)
- Pinyin with tone marks

English phrases:
{phrases_list}

Return ONLY valid JSON in this exact format:
{{
  "vocabulary": [
    {{"english": "phrase 1", "chinese": "中文翻译", "pinyin": "pīnyīn"}},
    {{"english": "phrase 2", "chinese": "中文翻译", "pinyin": "pīnyīn"}}
  ]
}}

Requirements:
1. Maintain phrase meaning precisely
2. Use natural Chinese expressions
3. HSK5 vocabulary level
4. Accurate pinyin with tone marks
5. ONLY valid JSON, no other text"""

    try:
        response = deepseek_client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": "Expert Chinese translator. Return only valid JSON."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.3,
            timeout=45.0
        )
        
        content_text = response.choices[0].message.content
        
        # Extract JSON
        json_match = re.search(r'\{.*\}', content_text, re.DOTALL)
        if json_match:
            content_text = json_match.group()
        
        content = json.loads(content_text)
        
        if 'vocabulary' not in content or not isinstance(content['vocabulary'], list):
            raise ValueError("Invalid response format")
        
        # Validate all items have required fields
        for item in content['vocabulary']:
            if not all(k in item for k in ['english', 'chinese', 'pinyin']):
                raise ValueError("Missing required fields in vocabulary item")
        
        print(f"[DeepSeek] ✅ Translated {len(content['vocabulary'])} phrases")
        return content['vocabulary']
        
    except json.JSONDecodeError as e:
        print(f"[ERROR] JSON parse: {str(e)}")
        print(f"[ERROR] Raw: {content_text[:200]}...")
        raise
    except Exception as e:
        print(f"[ERROR] DeepSeek: {type(e).__name__}: {str(e)}")
        raise

@retry(
    stop=stop_after_attempt(config.API_RETRY_ATTEMPTS),
    wait=wait_exponential(multiplier=1, min=4, max=10),
    retry=retry_if_exception_type((Exception,)),
    before_sleep=lambda retry_state: print(f"Retry {retry_state.attempt_number}: {retry_state.outcome.exception()}")
)
def generate_content_with_vocabulary(topic, vocabulary):
    """Generate main text and opinions using provided vocabulary"""
    print(f"[DeepSeek] Generating content for: {topic[:50]}...")
    
    vocab_list = "\n".join([f"- {item['chinese']} ({item['english']})" for item in vocabulary])
    
    prompt = f"""You are a world-class consultant writing in Mandarin Chinese.

Topic: "{topic}"

You MUST use ALL of these vocabulary items in your writing:
{vocab_list}

Create content in this JSON structure:
{{
  "main_text": "Main text in Simplified Chinese (300-400 characters) about the topic. MUST use ALL vocabulary items naturally.",
  "opinion_texts": {{
    "positive": "Positive perspective (150-200 chars) using 8-10 vocabulary items",
    "negative": "Critical perspective (150-200 chars) using 8-10 vocabulary items", 
    "balanced": "Balanced perspective (150-200 chars) using 8-10 vocabulary items"
  }}
}}

Requirements:
1. ALL vocabulary items MUST appear in main_text
2. Each opinion text uses 8-10 vocabulary items naturally
3. HSK5 level writing
4. Natural, engaging prose
5. ONLY valid JSON

The vocabulary items should be woven naturally into the text, not forced."""

    try:
        response = deepseek_client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": "Expert Chinese writer. HSK5 level. Return only valid JSON."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7,
            timeout=60.0
        )
        
        content_text = response.choices[0].message.content
        
        # Extract JSON
        json_match = re.search(r'\{.*\}', content_text, re.DOTALL)
        if json_match:
            content_text = json_match.group()
        
        content = json.loads(content_text)
        
        # Validate structure
        required_keys = ["main_text", "opinion_texts"]
        if not all(k in content for k in required_keys):
            raise ValueError(f"Missing required keys")
        
        if not all(k in content['opinion_texts'] for k in ['positive', 'negative', 'balanced']):
            raise ValueError("opinion_texts needs 'positive', 'negative', 'balanced'")
        
        print(f"[DeepSeek] ✅ Content generated successfully")
        return content
        
    except json.JSONDecodeError as e:
        print(f"[ERROR] JSON parse: {str(e)}")
        print(f"[ERROR] Raw: {content_text[:200]}...")
        raise
    except Exception as e:
        print(f"[ERROR] DeepSeek: {type(e).__name__}: {str(e)}")
        raise

async def create_vocabulary_file_with_tts(vocabulary, topic):
    """Create vocab file with TTS for Anki"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    topic_truncated = topic[:50] if len(topic) > 50 else topic
    safe_topic_name = safe_filename(topic_truncated)
    filename = f"{safe_topic_name}_{timestamp}_vocabulary.txt"
    
    content = ""
    audio_files = {}
    voice_info = []
    
    total_items = len(vocabulary)
    tts_tasks = []
    
    # Generate all TTS in parallel
    for item in vocabulary:
        tts_tasks.append(generate_tts_async(item['chinese'], voice_name=config.ANKI_VOICE, speaking_rate=0.8))
    
    audio_results = await asyncio.gather(*tts_tasks, return_exceptions=True)
    
    for idx, (item, result) in enumerate(zip(vocabulary, audio_results)):
        chinese_text = item['chinese']
        
        if isinstance(result, Exception):
            print(f"TTS failed for '{chinese_text}': {result}")
            voice_info.append(f"❌ {chinese_text}: FAILED - {str(result)[:50]}")
            content += f"{item['english']}\t{item['chinese']}\t{item['pinyin']}\n"
        elif isinstance(result, tuple):
            audio_data, voice_used, success = result
            if success and audio_data:
                hash_object = hashlib.md5(chinese_text.encode())
                audio_filename = f"tts_{hash_object.hexdigest()}.mp3"
                audio_filename = safe_filename(audio_filename)
                audio_files[audio_filename] = audio_data
                anki_tag = f"[sound:{audio_filename}]"
                content += f"{item['english']}\t{item['chinese']}\t{item['pinyin']}\t{anki_tag}\n"
                voice_info.append(f"✅ {chinese_text}: {voice_used}")
            else:
                voice_info.append(f"❌ {chinese_text}: FAILED - no audio")
                content += f"{item['english']}\t{item['chinese']}\t{item['pinyin']}\n"
        else:
            voice_info.append(f"❌ {chinese_text}: FAILED - unexpected result")
            content += f"{item['english']}\t{item['chinese']}\t{item['pinyin']}\n"
    
    return filename, content, audio_files, voice_info

def create_html_document(topic, vocabulary, content, timestamp):
    """Create HTML document"""
    topic_truncated = topic[:50] if len(topic) > 50 else topic
    safe_topic = safe_filename(topic_truncated)
    html_filename = f"{safe_topic}_{timestamp}_materials.html"
    
    # Vocabulary table
    vocab_rows = ""
    for i, item in enumerate(vocabulary, 1):
        vocab_rows += f"<tr><td>{i}</td><td>{item['chinese']}</td><td>{item['pinyin']}</td><td>{item['english']}</td></tr>\n"
    
    html_content = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{topic}</title>
    <style>
        body {{
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            max-width: 900px;
            margin: 40px auto;
            padding: 20px;
            line-height: 1.8;
            background: #f5f5f5;
        }}
        .header {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 30px;
            border-radius: 10px;
            margin-bottom: 30px;
            text-align: center;
        }}
        .header h1 {{
            margin: 0;
            font-size: 2em;
        }}
        .section {{
            background: white;
            padding: 30px;
            margin-bottom: 20px;
            border-radius: 10px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }}
        .section h2 {{
            color: #667eea;
            border-bottom: 3px solid #667eea;
            padding-bottom: 10px;
            margin-top: 0;
        }}
        .main-text {{
            font-size: 1.3em;
            line-height: 2;
            color: #333;
            text-align: justify;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            margin-top: 20px;
        }}
        th {{
            background: #667eea;
            color: white;
            padding: 12px;
            text-align: left;
            font-weight: 600;
        }}
        td {{
            padding: 12px;
            border-bottom: 1px solid #e0e0e0;
        }}
        tr:hover {{
            background: #f8f9ff;
        }}
        .opinion-box {{
            border-left: 4px solid #667eea;
            padding: 20px;
            margin: 15px 0;
            background: #f8f9ff;
            border-radius: 5px;
        }}
        .opinion-box.positive {{
            border-left-color: #4caf50;
            background: #f1f8f4;
        }}
        .opinion-box.negative {{
            border-left-color: #f44336;
            background: #fff3f3;
        }}
        .opinion-box.balanced {{
            border-left-color: #ff9800;
            background: #fff8f0;
        }}
        .opinion-label {{
            font-weight: bold;
            margin-bottom: 10px;
            font-size: 1.1em;
        }}
        .opinion-text {{
            font-size: 1.2em;
            line-height: 1.9;
            color: #333;
        }}
    </style>
</head>
<body>
    <div class="header">
        <h1>{topic}</h1>
    </div>
    
    <div class="section">
        <h2>📖 Main Text</h2>
        <div class="main-text">{content['main_text']}</div>
    </div>
    
    <div class="section">
        <h2>📝 Vocabulary</h2>
        <table>
            <thead>
                <tr>
                    <th>#</th>
                    <th>Chinese</th>
                    <th>Pinyin</th>
                    <th>English</th>
                </tr>
            </thead>
            <tbody>
                {vocab_rows}
            </tbody>
        </table>
    </div>
    
    <div class="section">
        <h2>💭 Perspectives</h2>
        
        <div class="opinion-box positive">
            <div class="opinion-label">✅ Positive View</div>
            <div class="opinion-text">{content['opinion_texts']['positive']}</div>
        </div>
        
        <div class="opinion-box negative">
            <div class="opinion-label">⚠️ Critical View</div>
            <div class="opinion-text">{content['opinion_texts']['negative']}</div>
        </div>
        
        <div class="opinion-box balanced">
            <div class="opinion-label">⚖️ Balanced View</div>
            <div class="opinion-text">{content['opinion_texts']['balanced']}</div>
        </div>
    </div>
</body>
</html>"""
    
    return html_filename, html_content

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    welcome_msg = """👋 Welcome to Custom Vocabulary Chinese Learning Bot!

📋 **How to use:**
1. Send me your vocabulary list (English-Russian format)
2. I'll ask for your topic
3. Send the topic
4. I'll create materials using YOUR vocabulary!

📦 **You'll receive:**
• HTML document with vocab table
• Main text using all your vocabulary
• 3 perspective texts (positive/critical/balanced)
• Audio files for listening practice
• Anki-ready vocabulary file with audio
• ZIP package with all audio files

Just paste your vocabulary list to begin!"""
    
    await update.message.reply_text(welcome_msg)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle vocabulary list or topic"""
    user_id = update.effective_user.id
    
    if not rate_limiter.is_allowed(user_id):
        reset_time = rate_limiter.get_reset_time(user_id)
        await update.message.reply_text(
            f"⏱️ Rate limit reached. Try again in {reset_time // 60} minutes."
        )
        return
    
    # Check if user has a session
    if user_id not in user_sessions:
        # This is vocabulary list
        try:
            vocab_list = parse_vocabulary_list(update.message.text)
            
            if not vocab_list:
                await update.message.reply_text(
                    "❌ No vocabulary found. Please send a list in format:\n"
                    "take up activities начать заниматься\n"
                    "figure out what drains выяснить что истощает\n"
                    "..."
                )
                return
            
            if len(vocab_list) > 30:
                await update.message.reply_text(
                    f"⚠️ Too many items ({len(vocab_list)}). Maximum is 30. Please reduce your list."
                )
                return
            
            # Store vocabulary and wait for topic
            user_sessions[user_id] = {
                'vocabulary_english': vocab_list,
                'waiting_for_topic': True
            }
            
            vocab_preview = "\n".join([f"• {v}" for v in vocab_list[:10]])
            if len(vocab_list) > 10:
                vocab_preview += f"\n... and {len(vocab_list) - 10} more"
            
            await update.message.reply_text(
                f"✅ Received {len(vocab_list)} vocabulary items:\n\n{vocab_preview}\n\n"
                f"📝 Now send me your topic!"
            )
            
        except Exception as e:
            print(f"[ERROR] Parsing vocab: {type(e).__name__}: {str(e)}")
            await update.message.reply_text(
                "❌ Error parsing vocabulary. Please check format and try again."
            )
    
    else:
        # This is the topic
        session = user_sessions[user_id]
        
        if not session.get('waiting_for_topic'):
            await update.message.reply_text(
                "⚠️ Please start over with /start"
            )
            del user_sessions[user_id]
            return
        
        try:
            topic = validate_topic(update.message.text)
            
            status_msg = await update.message.reply_text(
                "🔄 Processing your request...\n"
                "📝 Translating vocabulary to Chinese..."
            )
            
            # Step 1: Translate vocabulary
            vocabulary = translate_vocabulary_to_chinese(session['vocabulary_english'])
            
            await status_msg.edit_text(
                "✅ Vocabulary translated\n"
                "🔄 Generating content using your vocabulary..."
            )
            
            # Step 2: Generate content
            content = generate_content_with_vocabulary(topic, vocabulary)
            
            await status_msg.edit_text(
                "✅ Content generated\n"
                "🎙️ Creating audio files..."
            )
            
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_topic_name = safe_filename(topic[:50])
            
            # Step 3: Generate main text TTS
            await status_msg.edit_text("🎙️ Creating main text audio...")
            main_audio_data, main_voice_used, main_success = await generate_tts_async(
                content['main_text'],
                voice_name=None,
                speaking_rate=0.9
            )
            
            # Step 4: Generate opinion TTS
            await status_msg.edit_text("🎙️ Creating opinion audio files...")
            opinion_audio_data = {}
            opinion_voice_info = []
            
            for opinion_type in ['positive', 'negative', 'balanced']:
                opinion_text = content['opinion_texts'][opinion_type]
                audio_data, voice_used, success = await generate_tts_async(
                    opinion_text,
                    voice_name=None,
                    speaking_rate=0.9
                )
                if success and audio_data:
                    opinion_audio_data[opinion_type] = audio_data
                    opinion_voice_info.append(f"✅ {opinion_type}: {voice_used}")
                else:
                    opinion_voice_info.append(f"❌ {opinion_type}: FAILED")
            
            # Step 5: Create vocabulary with TTS
            await status_msg.edit_text("🎙️ Creating vocabulary audio...")
            vocab_filename, vocab_content, vocab_audio_files, vocab_voice_info = \
                await create_vocabulary_file_with_tts(vocabulary, topic)
            
            # Step 6: Create HTML
            await status_msg.edit_text("📝 Creating HTML document...")
            html_filename, html_content = create_html_document(topic, vocabulary, content, timestamp)
            
            # Send voice report
            voice_report = "🎙️ **TTS Voice Report**\n\n"
            if main_success:
                voice_report += f"**Main Text:** ✅ {main_voice_used}\n\n"
            else:
                voice_report += "**Main Text:** ❌ FAILED\n\n"
            voice_report += "**Opinion Texts:**\n" + "\n".join(opinion_voice_info)
            voice_report += f"\n\n**Vocabulary:** {len(vocab_audio_files)}/{len(vocabulary)} audio files created"
            
            await update.message.reply_text(voice_report)
            
            # Send files
            await status_msg.edit_text("📤 Sending files...")
            
            # 1. HTML
            html_buffer = BytesIO(html_content.encode('utf-8'))
            html_buffer.seek(0)
            await update.message.reply_document(
                document=html_buffer,
                filename=html_filename
            )
            
            # 2. Main text audio
            if main_success and main_audio_data:
                main_audio_filename = f"{safe_topic_name}_{timestamp}_main.mp3"
                main_audio_buffer = BytesIO(main_audio_data)
                main_audio_buffer.seek(0)
                await update.message.reply_audio(
                    audio=main_audio_buffer,
                    filename=main_audio_filename
                )
            
            # 3. Opinion audio files
            for opinion_type in ['positive', 'negative', 'balanced']:
                if opinion_type in opinion_audio_data:
                    audio_filename = f"{safe_topic_name}_{timestamp}_{opinion_type}.mp3"
                    audio_buffer = BytesIO(opinion_audio_data[opinion_type])
                    audio_buffer.seek(0)
                    await update.message.reply_audio(
                        audio=audio_buffer,
                        filename=audio_filename
                    )
            
            # 4. Anki vocabulary file
            vocab_buffer = BytesIO(vocab_content.encode('utf-8'))
            vocab_buffer.seek(0)
            await update.message.reply_document(
                document=vocab_buffer,
                filename=vocab_filename
            )
            
            # 5. ZIP with Anki audio
            await status_msg.edit_text("📦 Creating Anki audio package...")
            zip_buffer = BytesIO()
            with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
                for filename, audio_data in vocab_audio_files.items():
                    zip_file.writestr(filename, audio_data)
            
            zip_buffer.seek(0)
            zip_filename = f"{safe_topic_name}_{timestamp}_anki_audio.zip"
            await update.message.reply_document(
                document=zip_buffer,
                filename=zip_filename
            )
            
            await status_msg.delete()

            # Save last session to disk so /speak works even after restart
            save_last_session(user_id, {
                'topic': topic,
                'vocabulary': vocabulary,
                'content': content
            })

            # Clear session
            del user_sessions[user_id]
            
            await update.message.reply_text(
                "✅ All done! Send new vocabulary list to create more materials.\n\n"
                "💬 When you've done your Anki cards and listened to the audio, "
                "send /speak to practice speaking about this topic."
            )
            
        except ValueError as e:
            await update.message.reply_text(f"❌ Error: {str(e)}")
            if user_id in user_sessions:
                del user_sessions[user_id]
        except Exception as e:
            print(f"[ERROR] {type(e).__name__}: {str(e)}")
            import traceback
            traceback.print_exc()
            await update.message.reply_text(
                "❌ An error occurred. Please try again or contact support."
            )
            if user_id in user_sessions:
                del user_sessions[user_id]

# ─── SPEAKING MODE ────────────────────────────────────────────────────────────

SESSIONS_DIR = "speaking_sessions"

def save_last_session(user_id: int, data: dict):
    """Save topic, vocab and content to disk for /speak to use later."""
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    path = os.path.join(SESSIONS_DIR, f"{user_id}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"[SPEAK] Saved session for user {user_id}")

def load_last_session(user_id: int):
    """Load last saved session for user, or None if not found."""
    path = os.path.join(SESSIONS_DIR, f"{user_id}.json")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def generate_speaking_questions(topic: str, vocabulary: list, content: dict) -> list:
    """
    Generate 5 speaking questions in Chinese using vocab items and ideas from the text.
    """
    vocab_list = "\n".join([
        f"- {item['chinese']} ({item['pinyin']}) = {item['english']}"
        for item in vocabulary
    ])

    prompt = f"""You are a Chinese speaking tutor creating conversation questions for an HSK5 student.

Topic: {topic}

Main text summary: {content['main_text'][:300]}

Vocabulary available:
{vocab_list}

Create exactly 5 speaking questions in Chinese. Each question must:
1. Use 1-2 vocabulary items from the list naturally
2. Be based on an idea from the text
3. Be short and clear (max 25 Chinese characters)
4. Encourage a personal answer (not a yes/no dead end)
5. Follow one of these patterns:
   - 你同意...的观点吗？(Do you agree that...?)
   - ...这个观点在你的国家/经历中也成立吗？(Is this idea true in your experience?)
   - 你认识...的人吗？(Do you know anyone who...?)

Do NOT make assumptions about the student's life.
Vary the question types across the 5 questions.

Return ONLY valid JSON:
{{
  "questions": [
    {{
      "question_zh": "Chinese question",
      "question_en": "English translation",
      "vocab_used": ["chinese vocab item 1", "chinese vocab item 2"]
    }}
  ]
}}"""

    try:
        response = deepseek_client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": "Chinese speaking tutor. Return only valid JSON."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7,
            timeout=45.0
        )
        content_text = response.choices[0].message.content
        json_match = re.search(r'\{.*\}', content_text, re.DOTALL)
        if json_match:
            content_text = json_match.group()
        data = json.loads(content_text)
        questions = data.get("questions", [])
        print(f"[SPEAK] Generated {len(questions)} questions")
        return questions[:5]
    except Exception as e:
        print(f"[SPEAK ERROR] Question generation: {e}")
        return []

def generate_speaking_feedback(question_zh: str, question_en: str,
                                vocab_used: list, user_text: str) -> str:
    """Brief encouraging feedback on the student's spoken answer."""
    prompt = f"""You are a brief, encouraging Chinese speaking tutor.

Question asked (Chinese): {question_zh}
Question (English): {question_en}
Vocabulary the student should use: {', '.join(vocab_used)}
Student said (transcribed): {user_text}

Give brief feedback in English (max 3 sentences):
1. One encouraging comment
2. One specific correction or improvement if needed (grammar or vocab)
3. If they didn't use the target vocabulary, suggest how they could have

Be warm, concise, and specific. Do not repeat the full question."""

    try:
        response = deepseek_client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": "Brief, encouraging language tutor."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.6,
            timeout=30.0
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"[SPEAK ERROR] Feedback generation: {e}")
        return "Good effort! Keep practising."

def transcribe_voice_chinese(audio_path: str):
    """Transcribe voice using Google STT for Mandarin Chinese."""
    try:
        from google.cloud import speech as google_speech
        credentials_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
        credentials = service_account.Credentials.from_service_account_info(
            credentials_dict,
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        client = google_speech.SpeechClient(credentials=credentials)
        with open(audio_path, "rb") as f:
            audio_data = f.read()
        response = client.recognize(
            config=google_speech.RecognitionConfig(
                encoding=google_speech.RecognitionConfig.AudioEncoding.OGG_OPUS,
                sample_rate_hertz=48000,
                language_code="cmn-CN",
                alternative_language_codes=["cmn-Hans-CN"]
            ),
            audio=google_speech.RecognitionAudio(content=audio_data)
        )
        if response.results:
            return response.results[0].alternatives[0].transcript
    except Exception as e:
        print(f"[SPEAK STT ERROR] {e}")
    return None

async def speak_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/speak — start speaking practice on last generated topic."""
    user_id = update.effective_user.id

    session = load_last_session(user_id)
    if not session:
        await update.message.reply_text(
            "No previous session found. Please send a vocabulary list and topic first "
            "to generate materials, then use /speak."
        )
        return

    topic    = session["topic"]
    vocab    = session["vocabulary"]
    content  = session["content"]

    await update.message.reply_text(
        f"💬 Speaking practice — topic: {topic}\n\n"
        f"I'll ask you 5 questions in Chinese based on the text and vocabulary.\n"
        f"Answer each one with a voice message. Ready? Here we go! 🎤"
    )

    # Generate questions
    status = await update.message.reply_text("⏳ Generating questions...")
    questions = generate_speaking_questions(topic, vocab, content)

    if not questions:
        await status.edit_text("❌ Could not generate questions. Please try again.")
        return

    await status.delete()

    # Store speaking session in memory
    user_sessions[user_id] = {
        'mode': 'speaking',
        'questions': questions,
        'question_index': 0,
        'topic': topic,
        'vocab': vocab,
    }

    # Ask first question
    await ask_speaking_question(update.effective_chat.id, context, user_id)

async def ask_speaking_question(chat_id: int, context, user_id: int):
    """Send the current speaking question with TTS."""
    s         = user_sessions.get(user_id)
    if not s:
        return
    idx       = s['question_index']
    questions = s['questions']

    if idx >= len(questions):
        await context.bot.send_message(
            chat_id=chat_id,
            text="🎉 Speaking practice complete! Great work.\n\nSend a new vocab list any time to start again."
        )
        del user_sessions[user_id]
        return

    q = questions[idx]
    question_zh = q['question_zh']
    question_en = q['question_en']
    vocab_used  = q.get('vocab_used', [])

    # TTS for the question
    audio_data, _, success = await generate_tts_async(question_zh, speaking_rate=0.85)

    caption = (
        f"Question {idx + 1}/5\n\n"
        f"🇨🇳 {question_zh}\n"
        f"🇬🇧 {question_en}\n\n"
        f"Vocab to use: {', '.join(vocab_used)}\n\n"
        f"Reply with a voice message 🎤"
    )

    if success and audio_data:
        audio_buffer = BytesIO(audio_data)
        audio_buffer.seek(0)
        await context.bot.send_audio(
            chat_id=chat_id,
            audio=audio_buffer,
            filename=f"question_{idx+1}.mp3",
            caption=caption
        )
    else:
        await context.bot.send_message(chat_id=chat_id, text=caption)

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle voice messages during speaking practice."""
    user_id = update.effective_user.id
    s       = user_sessions.get(user_id)

    if not s or s.get('mode') != 'speaking':
        await update.message.reply_text(
            "Use /speak to start speaking practice first."
        )
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    # Download voice
    voice_file = await update.effective_message.voice.get_file()
    voice_path = f"/tmp/voice_{user_id}_{int(time.time())}.ogg"
    await voice_file.download_to_drive(voice_path)

    # Transcribe
    user_text = transcribe_voice_chinese(voice_path)
    os.remove(voice_path)

    if not user_text:
        await update.message.reply_text(
            "Could not understand the audio. Please try again 🎤\n"
            "(Make sure you're speaking clearly in Mandarin Chinese)"
        )
        return

    # Get current question
    idx = s['question_index']
    q   = s['questions'][idx]

    # Feedback
    feedback = generate_speaking_feedback(
        question_zh=q['question_zh'],
        question_en=q['question_en'],
        vocab_used=q.get('vocab_used', []),
        user_text=user_text
    )

    await update.message.reply_text(
        f"You said: {user_text}\n\n"
        f"💬 {feedback}"
    )

    # Advance to next question
    s['question_index'] += 1
    if s['question_index'] < len(s['questions']):
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"Next question coming up..."
        )
        await ask_speaking_question(update.effective_chat.id, context, user_id)
    else:
        await update.message.reply_text(
            "🎉 Speaking practice complete! Great work.\n\n"
            "Send a new vocab list any time to create more materials."
        )
        del user_sessions[user_id]


def main():
    """Start the bot"""
    if not TELEGRAM_BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN not set")
        return
    
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("speak", speak_command))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    print("🤖 Vocabulary-Based Chinese Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
