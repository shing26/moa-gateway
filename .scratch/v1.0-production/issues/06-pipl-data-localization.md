# PIPL Êý¾Ý±¾µØ»¯

> wayfinder:grilling
> status: open
> blocking: 07, 08

## Question

·þÎñÆ÷·ÅÄÄ¸ö Region£¿ÈÕÖ¾´æÄÄÀï£¿ÊÇ·ñÐèÒªÓÃ»§Êý¾Ýµ¼³ö/É¾³ý API£¿

## Context

- PIPL ÒªÇó¸öÈËÐÅÏ¢±¾µØ»¯´æ´¢
- ÓÃ»§ÓÐÈ¨ÒªÇóÉ¾³ý¸öÈËÊý¾Ý
- µ±Ç°ÓÃ»§Êý¾Ý´æÔÚ VectorDB ºÍÈÕÖ¾ÖÐ

## Options

A) ËùÓÐÊý¾Ý´æ±¾µØ + ÊµÏÖ DELETE /privacy/user/{id} API
B) Í¬ A + Ôö¼ÓÓÃ»§Í¬Òâ¼ÇÂ¼
C) ½öÊµÏÖÉ¾³ý API£¬Í¬ÒâÓÉÓ¦ÓÃ²ã¹ÜÀí

## Resolution

<!-- ½â¾öºóÌîÐ´ -->


## Resolution

**Decision**: æœåŠ¡å™¨æ”¾å›½å†… + DELETE /privacy/user/{id} API + æ—¥å¿— TTL
- æ•°æ®æœ¬åœ°åŒ–: æœåŠ¡å™¨åœ¨å›½å†…å°±è¡Œï¼Œé›¶æ”¹åŠ¨
- è¢«é—å¿˜æƒ: å®žçŽ° DELETE /api/v1/privacy/user/{user_id}
- æ—¥å¿—æ¸…ç†: é…ç½®æ—¥å¿— TTLï¼ˆå»ºè®® 90 å¤©ï¼‰
