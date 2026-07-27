/*
 * Service worker: makes the UI survive a reload with no network.
 *
 * Only the shell is cached. Anything under /api or /ws goes to the network and is
 * never cached or served stale -- showing a previous car's report, or last week's
 * fault codes, because a request failed would be actively dangerous. A visible error
 * is always the better outcome than a plausible wrong answer.
 */
'use strict';

// Bump this when a shell asset changes; the old cache is deleted on activate.
const CACHE = 'carpi-shell-v1';

const SHELL = [
  './',
  'index.html',
  'style.css',
  'app.js',
  'icon.svg',
  'manifest.webmanifest',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting()),
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((names) => Promise.all(names.filter((n) => n !== CACHE).map((n) => caches.delete(n))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener('fetch', (event) => {
  const { request } = event;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  // Never intercept vehicle data. See the note at the top of this file.
  if (url.pathname.startsWith('/api') || url.pathname.startsWith('/ws')) return;

  event.respondWith(
    caches.match(request).then((cached) => {
      if (cached) return cached;
      return fetch(request)
        .then((response) => {
          // Only same-origin successes are worth keeping.
          if (response.ok && response.type === 'basic') {
            const copy = response.clone();
            caches.open(CACHE).then((cache) => cache.put(request, copy));
          }
          return response;
        })
        .catch(() => caches.match('index.html'));
    }),
  );
});
