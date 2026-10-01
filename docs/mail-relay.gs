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
    return respond({ ok: true, remaining: MailApp.getRemainingDailyQuota() });
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
