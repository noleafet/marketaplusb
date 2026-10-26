import { NextRequest, NextResponse } from 'next/server';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

const BACKEND = (process.env.ENGINE_BACKEND_URL ?? 'http://localhost:8000').replace(/\/$/, '');
const TOKEN = process.env.ENGINE_API_TOKEN ?? '';

async function proxy(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  const { path } = await context.params;
  const pathname = path.join('/');
  const suffix = pathname.endsWith('/') ? '' : '/';
  const target = new URL(`${BACKEND}/api/${pathname}${suffix}`);
  target.search = request.nextUrl.search;

  const headers = new Headers({ accept: 'application/json' });
  if (TOKEN) {
    headers.set('X-Engine-Token', TOKEN);
  }
  const contentType = request.headers.get('content-type');
  if (contentType) {
    headers.set('content-type', contentType);
  }

  const init: RequestInit = {
    method: request.method,
    headers,
    cache: 'no-store',
    redirect: 'manual'
  };

  if (!['GET', 'HEAD'].includes(request.method)) {
    init.body = await request.text();
  }

  try {
    const response = await fetch(target, init);
    const body = await response.text();
    const responseHeaders = new Headers();
    const responseType = response.headers.get('content-type');
    if (responseType) {
      responseHeaders.set('content-type', responseType);
    }
    if (response.status === 301 || response.status === 302 || response.status === 308) {
      responseHeaders.set('location', response.headers.get('location') ?? target.toString());
    }
    return new NextResponse(body, {
      status: response.status,
      headers: responseHeaders
    });
  } catch (error) {
    return NextResponse.json(
      {
        detail: 'Engine API is unreachable.',
        error: error instanceof Error ? error.message : String(error)
      },
      { status: 502 }
    );
  }
}

export { proxy as GET, proxy as POST, proxy as PATCH, proxy as PUT, proxy as DELETE };
