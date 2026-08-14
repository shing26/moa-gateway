from app.agents.stubs import CoderAgent, GeneralAgent
from app.agents.review_agent import ReviewAgent
from app.agents.contract import register_agent, get_agent
if get_agent('coder') is None:
    register_agent('coder', CoderAgent())
    register_agent('general', GeneralAgent())
    register_agent('review', ReviewAgent())
    import logging; logging.getLogger('moa.agents').warning('agents registered lazily')
