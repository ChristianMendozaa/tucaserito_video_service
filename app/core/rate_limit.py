from slowapi import Limiter
from slowapi.util import get_remote_address

# This uses the client's IP address to apply the rate limits
limiter = Limiter(key_func=get_remote_address)
