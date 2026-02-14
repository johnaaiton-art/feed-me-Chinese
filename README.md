# Feed Me Chinese Bot 🇨🇳

A Telegram bot that creates personalized Chinese learning materials based on your custom vocabulary list.

## Features

- 📝 **Custom Vocabulary**: Paste your English vocabulary list, bot translates to Chinese with pinyin
- 📖 **Contextual Learning**: Main text and opinion pieces use YOUR vocabulary naturally
- 🎙️ **Audio Files**: TTS audio for all texts using Google's Chirp3 voices
- 📦 **Anki-Ready**: Tab-delimited vocabulary file with audio tags for Anki import
- 🌐 **HTML Materials**: Beautiful HTML document for offline study

## How It Works

1. **Send vocabulary list** (English-Russian format, bot extracts English)
   ```
   take up activities	начать заниматься
   figure out what drains	выяснить что истощает
   cut back on commitments	сократить обязательства
   ```

2. **Bot asks for topic** - Send your topic, e.g.:
   ```
   How to discover new sources of life energy and enhance vitality
   ```

3. **Receive materials**:
   - HTML document with vocab table
   - Main text (300-400 characters) using ALL vocabulary
   - 3 perspective texts (positive, critical, balanced)
   - Audio files for listening practice
   - Anki vocabulary file with [sound:] tags
   - ZIP package with all vocabulary audio

## Generated Content

### Vocabulary Table
| # | Chinese | Pinyin | English |
|---|---------|--------|---------|
| 1 | 开始从事活动 | kāishǐ cóngshì huódòng | take up activities |
| ... | ... | ... | ... |

### Main Text
HSK5-level text about your topic using ALL vocabulary items naturally.

### Perspective Texts
- ✅ **Positive View**: 150-200 characters
- ⚠️ **Critical View**: 150-200 characters  
- ⚖️ **Balanced View**: 150-200 characters

All perspective texts recycle the vocabulary items.

## Setup

### Prerequisites
- Python 3.8+
- Telegram Bot Token
- DeepSeek API Key
- Google Cloud TTS credentials

### Installation

1. Clone the repository:
```bash
git clone https://github.com/johnaaiton-art/feed-me-Chinese.git
cd feed-me-Chinese
```

2. Create virtual environment:
```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

3. Install dependencies:
```bash
pip install -r requirements.txt
```

4. Set up credentials:
   - Create `.env` file with:
     ```
     TELEGRAM_BOT_TOKEN=your_telegram_token
     DEEPSEEK_API_KEY=your_deepseek_key
     ```
   - Add `google-creds.json` with your Google Cloud service account key

5. Run the bot:
```bash
python vocab_based_chinese_bot.py
```

## Deployment (Yandex Cloud VM)

See [DEPLOYMENT.md](DEPLOYMENT.md) for detailed instructions.

## Tech Stack

- **Telegram Bot**: python-telegram-bot
- **LLM**: DeepSeek Chat API
- **TTS**: Google Cloud Text-to-Speech (Chirp3 HD voices)
- **Languages**: Simplified Chinese (HSK5 level)

## Rate Limits

- 5 requests per user per hour
- Maximum 30 vocabulary items per request

## File Structure

```
feed-me-chinese/
├── vocab_based_chinese_bot.py    # Main bot code
├── requirements.txt               # Python dependencies
├── .env                          # Environment variables (not in git)
├── google-creds.json             # Google credentials (not in git)
├── .gitignore                    # Git ignore rules
├── README.md                     # This file
├── DEPLOYMENT.md                 # Deployment guide
└── systemd/
    └── feed-me-chinese.service   # Systemd service file
```

## Contributing

This is a personal project for language learning. Feel free to fork and adapt!

## License

MIT License - see LICENSE file for details

## Author

Borja - English language educator and educational technology developer

## Acknowledgments

- DeepSeek for the powerful LLM API
- Google Cloud for Chirp3 TTS voices
- Telegram for the bot platform
