const SECRET = 'CHANGE_ME';

function doPost(e) {
  const respond = (value) => ContentService.createTextOutput(JSON.stringify(value))
    .setMimeType(ContentService.MimeType.JSON);
  let payload;
  try {
    payload = JSON.parse(e.postData.contents);
  } catch (_) {
    return respond({ ok: false, error: 'Invalid JSON' });
  }
  if (!payload || payload.secret !== SECRET) {
    return respond({ ok: false, error: 'Unauthorized' });
  }
  if (payload.ping === true) {
    return respond({ ok: true, remaining: MailApp.getRemainingDailyQuota(), fetch: true });
  }
  if (payload.fetch) {
    return respond(fetchTikTok(String(payload.fetch)));
  }
  if (!payload.to || !payload.subject || !payload.html) {
    return respond({ ok: false, error: 'Missing mail fields' });
  }
  try {
    MailApp.sendEmail({
      to: payload.to,
      subject: payload.subject,
      body: payload.text || payload.subject,
      htmlBody: payload.html,
      name: payload.name || 'Unprompted',
    });
    return respond({ ok: true, remaining: MailApp.getRemainingDailyQuota() });
  } catch (err) {
    return respond({ ok: false, error: String(err) });
  }
}

// Public TikTok pages only: the server's own requests are sometimes refused by TikTok, Google's are not.
const TIKTOK_URL = /^https:\/\/www\.tiktok\.com\/(embed\/@[\w.]{1,40}|@[\w.]{1,40}\/video\/\d{1,25})$/;

function fetchTikTok(url) {
  if (!TIKTOK_URL.test(url)) {
    return { ok: false, error: 'URL not allowed' };
  }
  try {
    const r = UrlFetchApp.fetch(url, {
      muteHttpExceptions: true,
      followRedirects: true,
      headers: {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36',
        'Accept-Language': 'en-US,en;q=0.9',
      },
    });
    return { ok: true, status: r.getResponseCode(), body: r.getContentText() };
  } catch (err) {
    return { ok: false, error: String(err) };
  }
}
