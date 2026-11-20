# Yuta Bot — Telegram 18+ Content Auto-Remover

Group me bheje gaye stickers/photos ko scan karta hai aur 18+ (NSFW) content
milne par turant delete kar deta hai. Detection poori tarah local model
(NudeNet) se hota hai — koi paid API nahi, koi daily/monthly limit nahi.

## Setup

1. **BotFather se token lo**
   Telegram me @BotFather ko message karo, `/newbot` se naya bot banao,
   token copy karo.

2. **Dependencies install karo**
   ```
   pip install -r requirements.txt
   ```
   (Pehli baar chalne par NudeNet apna model file khud download karega —
   internet chahiye hoga sirf first run me.)

3. **Token set karo**
   ```
   export BOT_TOKEN="123456:ABC-your-token"
   ```

4. **Bot chalao**
   ```
   python bot.py
   ```

5. **Group me setup**
   - Bot ko group me add karo
   - Bot ko **Admin** banao
   - Sirf **"Delete Messages"** permission on karo (baaki optional)

Bas. Ab bot 24/7 har sticker/photo scan karega, NSFW milte hi delete kar dega.

## Settings (bot.py ke top par)

- `NSFW_LABELS` — kaunse labels ko "18+" maanna hai (default: exposed body parts)
- `CONFIDENCE_THRESHOLD` — kitna confident model ko hona chahiye (0.55 default;
  badhane se false-positive kam honge, ghatane se detection zyada strict hoga)

## Hosting (free)

- **FadeHost** ya jo bhi free host pehle se use ho raha hai — pehle RAM check
  karo (NudeNet ko kam se kam ~300-500MB chahiye).
- **Oracle Cloud Always Free** — zyada reliable, 1GB+ RAM free milta hai,
  heavy model ke liye better.

## Note

- **Video stickers (.webm)** ab bhi scan hote hain — bot unka pehla frame
  nikal kar check karta hai. Iske liye host par **ffmpeg installed** hona
  zaroori hai (zyada free hosts me already hota hai; agar nahi hai to host
  ke package manager se install karna hoga, ya logs me "ffmpeg not installed"
  error dikhega).
- **Animated (.tgs / Lottie) stickers** abhi bhi skip hote hain — ye format
  alag hai aur frame extraction ka tareeka alag chahiye.
- Bot ke paas "Delete Messages" permission na ho to delete fail hoga
  (log me error dikhega).
