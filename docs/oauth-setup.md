# Creator handle OAuth setup

OAuth verification stays off until the provider's client ID/key and secret are both configured.

## TikTok

1. Create a TikTok developer app and add **Login Kit**.
2. Request the `user.info.basic` and `user.info.profile` scopes.
3. Add this exact redirect URI to the app:
   `https://receipts-thc8.onrender.com/auth/tiktok/callback/`
4. Submit the app for review. TikTok approval is required before people outside the app's developer/tester accounts can use Login Kit.
5. In the Receipts Render service, set `TIKTOK_CLIENT_KEY` and `TIKTOK_CLIENT_SECRET`.

## Instagram

1. In Meta for Developers, create or select an app and add **Instagram API with Instagram Login**.
2. Request the `instagram_business_basic` permission.
3. Add this exact redirect URI:
   `https://receipts-thc8.onrender.com/auth/instagram/callback/`
4. Request Advanced Access and complete app review before allowing accounts the app owner does not manage to connect.
5. In the Receipts Render service, set `INSTAGRAM_APP_ID` and `INSTAGRAM_APP_SECRET`.

After setting or changing credentials, redeploy the Render service. Each provider appears on the claim page only when both of its environment variables are present. The bio-code and operator-approval verification paths remain available as fallbacks.
