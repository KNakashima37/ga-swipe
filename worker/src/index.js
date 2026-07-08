// Cloudflare Worker: DeepL API を中継する薄いプロキシ
//
// エンドポイント:
//   POST /translate  { text, id } を受け取り英→日翻訳して { id, text_ja, cached } を返す（本文タップ翻訳用）
//   GET  /usage       DeepLの当月使用量 { character_count, character_limit } を返す
//
// バインディング/環境変数（wrangler.toml もしくは `wrangler secret put` で設定。README参照）:
//   DEEPL_API_KEY   (secret) DeepL APIキー。レスポンス・エラーメッセージに一切含めない
//   ALLOWED_ORIGIN  (var)    CORSを許可する自分のGitHub Pagesのオリジン（例: https://user.github.io）
//   TRANSLATE_CACHE (KV)     論文ID単位の翻訳結果の永続キャッシュ。同じ論文を二度翻訳しないための本体
//   RATE_LIMIT_KV   (KV)     同一IPからの過度な連打を防ぐ簡易カウンタ

const RATE_LIMIT_WINDOW_SEC = 60;
const RATE_LIMIT_MAX = 20;          // 同一IPからこのWindow内に許可する最大リクエスト数
const MAX_TEXT_LEN = 6000;          // アブスト1本の想定上限より十分大きい安全マージン
const ARXIV_ID_RE = /^\d{4}\.\d{4,5}$/;

function corsHeaders(origin, allowedOrigin){
  const h = {
    'Access-Control-Allow-Methods': 'POST, GET, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type',
    'Vary': 'Origin',
  };
  if(allowedOrigin && origin === allowedOrigin){
    h['Access-Control-Allow-Origin'] = allowedOrigin;
  }
  return h;
}

function json(status, body, extraHeaders){
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', ...(extraHeaders || {}) },
  });
}

// KV書き込みは即時一貫ではないため厳密なレート制限ではないが、
// 単純な連打防止としては十分（KV未設定ならフェイルオープンで翻訳自体は継続）
async function checkRateLimit(env, ip){
  if(!env.RATE_LIMIT_KV) return true;
  const key = 'rl:' + ip;
  const now = Math.floor(Date.now() / 1000);
  const raw = await env.RATE_LIMIT_KV.get(key);
  let entry = raw ? JSON.parse(raw) : null;
  if(!entry || now - entry.start >= RATE_LIMIT_WINDOW_SEC){
    entry = { start: now, count: 0 };
  }
  entry.count += 1;
  await env.RATE_LIMIT_KV.put(key, JSON.stringify(entry), { expirationTtl: RATE_LIMIT_WINDOW_SEC * 2 });
  return entry.count <= RATE_LIMIT_MAX;
}

function apiHost(apiKey){
  // 無料プランのキーは末尾 :fx で、APIのホストが有料プランと異なる
  return apiKey.endsWith(':fx') ? 'https://api-free.deepl.com' : 'https://api.deepl.com';
}

async function deeplTranslate(env, text){
  const res = await fetch(apiHost(env.DEEPL_API_KEY) + '/v2/translate', {
    method: 'POST',
    headers: {
      'Authorization': 'DeepL-Auth-Key ' + env.DEEPL_API_KEY,
      'Content-Type': 'application/x-www-form-urlencoded',
    },
    body: new URLSearchParams({ text, source_lang: 'EN', target_lang: 'JA' }),
  });
  if(!res.ok) throw new Error('deepl_error_' + res.status);
  const data = await res.json();
  return data.translations && data.translations[0] && data.translations[0].text;
}

async function deeplUsage(env){
  const res = await fetch(apiHost(env.DEEPL_API_KEY) + '/v2/usage', {
    headers: { 'Authorization': 'DeepL-Auth-Key ' + env.DEEPL_API_KEY },
  });
  if(!res.ok) throw new Error('deepl_error_' + res.status);
  const data = await res.json();
  return { character_count: data.character_count, character_limit: data.character_limit };
}

async function handleTranslate(req, env){
  let payload;
  try{ payload = await req.json(); }catch(e){ return json(400, { error: 'invalid_json' }); }
  const { text, id } = payload || {};
  if(typeof text !== 'string' || !text.trim()) return json(400, { error: 'text_required' });
  if(text.length > MAX_TEXT_LEN) return json(400, { error: 'text_too_long' });
  if(typeof id !== 'string' || !ARXIV_ID_RE.test(id)) return json(400, { error: 'invalid_id' });

  const cacheKey = 'abs:' + id;
  if(env.TRANSLATE_CACHE){
    const cached = await env.TRANSLATE_CACHE.get(cacheKey);
    if(cached) return json(200, { id, text_ja: cached, cached: true });
  }

  let translated;
  try{
    translated = await deeplTranslate(env, text);
  }catch(e){
    // DeepL側のエラー詳細（APIキーを含みうる）はそのままクライアントへ返さない
    return json(502, { error: 'translate_failed' });
  }
  if(!translated) return json(502, { error: 'translate_failed' });

  if(env.TRANSLATE_CACHE){
    await env.TRANSLATE_CACHE.put(cacheKey, translated);
  }
  return json(200, { id, text_ja: translated, cached: false });
}

async function handleUsage(env){
  try{
    return json(200, await deeplUsage(env));
  }catch(e){
    return json(502, { error: 'usage_failed' });
  }
}

export default {
  async fetch(req, env){
    const url = new URL(req.url);
    const origin = req.headers.get('Origin') || '';
    const cors = corsHeaders(origin, env.ALLOWED_ORIGIN);

    if(req.method === 'OPTIONS'){
      return new Response(null, { status: 204, headers: cors });
    }

    // 許可オリジン以外は拒否（CORSヘッダも付けないので、ブラウザ側でも二重に弾かれる）
    if(env.ALLOWED_ORIGIN && origin !== env.ALLOWED_ORIGIN){
      return json(403, { error: 'forbidden_origin' });
    }

    let res;
    if(url.pathname === '/translate' && req.method === 'POST'){
      const ip = req.headers.get('CF-Connecting-IP') || 'unknown';
      const allowed = await checkRateLimit(env, ip);
      res = allowed ? await handleTranslate(req, env) : json(429, { error: 'rate_limited' });
    } else if(url.pathname === '/usage' && req.method === 'GET'){
      const ip = req.headers.get('CF-Connecting-IP') || 'unknown';
      const allowed = await checkRateLimit(env, ip);
      res = allowed ? await handleUsage(env) : json(429, { error: 'rate_limited' });
    } else {
      res = json(404, { error: 'not_found' });
    }

    const headers = new Headers(res.headers);
    Object.entries(cors).forEach(([k, v]) => headers.set(k, v));
    return new Response(res.body, { status: res.status, headers });
  },
};
