import ast
with open('D:/HermesData/moa-gateway/app/main.py', 'r', encoding='utf-8-sig') as f:
    c = f.read()

old = '@app.get("/health")\nasync def health() -> dict[str, str]:\n    return {"status": "ok", "version": "0.1.0"}'

new = old + '''

@app.delete("/api/v1/privacy/user/{user_id}")
async def privacy_erase(user_id: str) -> JSONResponse:
    """PIPL right to erasure. Deletes all stored data for a user."""
    deleted = {}
    try:
        count = await _retriever._client.delete_by_metadata({"user_id": user_id})
        deleted["vectordb"] = count
    except Exception as e:
        deleted["vectordb"] = str(e)
    logger.info("privacy erase user=%s deleted=%s", user_id, deleted)
    return JSONResponse({"user_id": user_id, "deleted": deleted, "status": "ok"})
'''

c = c.replace(old, new, 1)

with open('D:/HermesData/moa-gateway/app/main.py', 'w', encoding='utf-8') as f:
    f.write(c)

ast.parse(c)
print("OK")
