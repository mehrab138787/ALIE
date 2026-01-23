// static/service-worker.js
const CACHE_NAME = 'cyrus-ai-cache-v2';
const urlsToCache = [
  '/',
  '/static/manifest.json',
  '/offline.html',
  // اگر فایل css یا js جداگانه دارید اینجا اضافه کنید
];

// نصب سرویس ورکر و کش کردن فایل‌ها
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => {
        console.log('Opened cache');
        return cache.addAll(urlsToCache);
      })
  );
});

// استراتژی: اول شبکه، اگر نشد (آفلاین بود)، از کش بخوان
self.addEventListener('fetch', event => {
  event.respondWith(
    fetch(event.request)
      .catch(() => {
        return caches.match(event.request).then(response => {
            if (response) {
                return response;
            }
            // اگر فایل در کش نبود و اینترنت هم قطع بود، صفحه آفلاین را نشان بده
            if (event.request.mode === 'navigate') {
                return caches.match('/offline.html');
            }
        });
      })
  );
});

// پاک کردن کش‌های قدیمی هنگام آپدیت
self.addEventListener('activate', event => {
  const cacheWhitelist = [CACHE_NAME];
  event.waitUntil(
    caches.keys().then(cacheNames => {
      return Promise.all(
        cacheNames.map(cacheName => {
          if (cacheWhitelist.indexOf(cacheName) === -1) {
            return caches.delete(cacheName);
          }
        })
      );
    })
  );
});