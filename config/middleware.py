# config/middleware.py
#
# NgrokSkipWarningMiddleware
# --------------------------
# Problem: Fyers callback URL pe ngrok free tier ek browser warning page dikhata hai
#          "You are about to visit: kangaroo-manpower-issuing.ngrok-free.dev"
#          "Visit Site" button ke saath — Fyers redirect yahan ruk jaata tha.
#
# Fix: Har Django response mein "ngrok-skip-browser-warning: true" header add karo.
#      Jab Fyers is URL pe redirect karta hai, ngrok header dekh ke warning skip karta hai
#      aur seedha Django view tak pahuncha deta hai.
#
# Reference: https://ngrok.com/docs/using-ngrok/browser-protection/

class NgrokSkipWarningMiddleware:
    """
    Har HTTP response mein ngrok-skip-browser-warning header add karta hai.
    Sirf development/ngrok environment mein use karo.
    Production mein yeh header harmless hai (servers ignore karte hain).
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response["ngrok-skip-browser-warning"] = "true"
        return response