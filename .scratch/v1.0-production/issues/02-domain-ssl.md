# ÓòÃû/SSL/CDN

> wayfinder:research
> status: blocked
> blocked_by: 01

## Question

ĞèÒªÊ²Ã´ÓòÃû£¿SSL Ö¤ÊéÔõÃ´Åª£¿ÊÇ·ñĞèÒª CDN£¿

## Context

- ·ÉÊé Webhook »Øµ÷ĞèÒª HTTPS + ¹«Íø¿É´ï
- Let's Encrypt Ãâ·Ñ SSL
- Èç¹ûÊÇµ¥»ú²¿Êğ£¬¿ÉÒÔÓÃ frp »òÆäËû´©Í¸¹¤¾ß

## Resolution

<!-- ½â¾öºóÌîĞ´ -->


## Resolution

**Decision**: Cloudflare Tunnel quick tunnel for local testing
- Tool: cloudflared.exe (å·²å®‰è£…)
- Tunnel URL: https://walking-ron-shorts-planes.trycloudflare.com
- Transport: HTTP/2 (QUIC fallback)
- Status: âœ… /health returns 200

**Steps to reproduce**:
1. Start Gateway: python -m uvicorn app.main:app --host 0.0.0.0 --port 8080
2. Start tunnel: .\cloudflared.exe tunnel --url http://localhost:8080
3. Use URL: https://xxx.trycloudflare.com/webhook/feishu

**Note**: tunnel URL changes on each restart. For permanent URL, create named Cloudflare Tunnel with a domain.
