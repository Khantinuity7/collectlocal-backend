/**
 * CollectLocal â Restock Push Notification Edge Function
 * ======================================================
 * Called by restock_checker.py when a restock event is detected.
 *
 * Flow:
 * 1. Receives restock event IDs
 * 2. Looks up event details (product + store)
 * 3. Finds users on the waitlist for that product within radius
 * 4. Sends FCM push notifications to matching users
 * 5. Logs notifications sent
 *
 * Cost: $0 (Supabase Edge Functions free tier: 500K invocations/month)
 *       FCM (Firebase Cloud Messaging) is completely free, unlimited
 */

import { serve } from "https://deno.land/std@0.177.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SUPABASE_SERVICE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
// FCM V1 API â uses service account JSON (base64-encoded) for OAuth2
const FCM_SERVICE_ACCOUNT_B64 = Deno.env.get("FCM_SERVICE_ACCOUNT") || "";
const FCM_PROJECT_ID = "collectlocal-57a42";

const supabase = createClient(SUPABASE_URL, SUPABASE_SERVICE_KEY);

// ââ JWT / OAuth2 helpers for FCM V1 âââââââââââââââââââââââââââââ

function base64UrlEncode(data: Uint8Array): string {
  return btoa(String.fromCharCode(...data))
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
}

async function createJWT(serviceAccount: { client_email: string; private_key: string }): Promise<string> {
  const header = { alg: "RS256", typ: "JWT" };
  const now = Math.floor(Date.now() / 1000);
  const payload = {
    iss: serviceAccount.client_email,
    scope: "https://www.googleapis.com/auth/firebase.messaging",
    aud: "https://oauth2.googleapis.com/token",
    iat: now,
    exp: now + 3600,
  };

  const enc = new TextEncoder();
  const headerB64 = base64UrlEncode(enc.encode(JSON.stringify(header)));
  const payloadB64 = base64UrlEncode(enc.encode(JSON.stringify(payload)));
  const unsignedToken = `${headerB64}.${payloadB64}`;

  // Import the RSA private key
  const pemBody = serviceAccount.private_key
    .replace(/-----BEGIN PRIVATE KEY-----/, "")
    .replace(/-----END PRIVATE KEY-----/, "")
    .replace(/\n/g, "");
  const binaryKey = Uint8Array.from(atob(pemBody), (c) => c.charCodeAt(0));

  const cryptoKey = await crypto.subtle.importKey(
    "pkcs8",
    binaryKey,
    { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" },
    false,
    ["sign"]
  );

  const signature = new Uint8Array(
    await crypto.subtle.sign("RSASSA-PKCS1-v1_5", cryptoKey, enc.encode(unsignedToken))
  );

  return `${unsignedToken}.${base64UrlEncode(signature)}`;
}

let _cachedAccessToken: { token: string; expiresAt: number } | null = null;

async function getAccessToken(): Promise<string> {
  if (_cachedAccessToken && Date.now() < _cachedAccessToken.expiresAt) {
    return _cachedAccessToken.token;
  }

  const saJson = JSON.parse(atob(FCM_SERVICE_ACCOUNT_B64));
  const jwt = await createJWT(saJson);

  const resp = await fetch("https://oauth2.googleapis.com/token", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: `grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer&assertion=${jwt}`,
  });

  const data = await resp.json();
  _cachedAccessToken = {
    token: data.access_token,
    expiresAt: Date.now() + (data.expires_in - 60) * 1000,
  };
  return data.access_token;
}

interface RestockEvent {
  id: string;
  store_id: string;
  product_id: string;
  new_quantity: number;
  source: string;
}

interface WaitlistEntry {
  id: string;
  user_id: string;
  product_id: string;
  radius_miles: number;
  retailers: string[];
  notify_push: boolean;
}

interface DeviceToken {
  fcm_token: string;
  user_id: string;
  lat: number | null;
  lng: number | null;
}

serve(async (req) => {
  try {
    const { event_ids } = await req.json();

    if (!event_ids || !event_ids.length) {
      return new Response(JSON.stringify({ error: "No event IDs provided" }), {
        status: 400,
        headers: { "Content-Type": "application/json" },
      });
    }

    console.log(`Processing ${event_ids.length} restock events...`);

    // 1. Fetch restock events with product and store details
    const { data: events, error: eventsError } = await supabase
      .from("restock_events")
      .select("*")
      .in("id", event_ids);

    if (eventsError || !events?.length) {
      console.error("Failed to fetch events:", eventsError);
      return new Response(JSON.stringify({ error: "Events not found" }), {
        status: 404,
        headers: { "Content-Type": "application/json" },
      });
    }

    let totalSent = 0;

    for (const event of events as RestockEvent[]) {
      // 2. Get product details
      const { data: product } = await supabase
        .from("restock_products")
        .select("*")
        .eq("id", event.product_id)
        .single();

      // 3. Get store details
      const { data: store } = await supabase
        .from("retail_stores")
        .select("*")
        .eq("id", event.store_id)
        .single();

      if (!product || !store) {
        console.error(`Product or store not found for event ${event.id}`);
        continue;
      }

      // 4. Find waitlist users for this product
      const { data: waitlist } = await supabase
        .from("restock_waitlist")
        .select("*")
        .eq("product_id", event.product_id)
        .eq("notify_push", true)
        .contains("retailers", [store.retailer]);

      if (!waitlist?.length) {
        console.log(`No waitlist users for ${product.name} at ${store.name}`);
        continue;
      }

      console.log(`Found ${waitlist.length} waitlist users for ${product.name}`);

      // 5. For each waitlist user, check if store is within their radius
      for (const entry of waitlist as WaitlistEntry[]) {
        // Get user's device tokens with location
        const { data: tokens } = await supabase
          .from("device_tokens")
          .select("*")
          .eq("user_id", entry.user_id);

        if (!tokens?.length) continue;

        // Check if any device is within the user's configured radius
        const withinRadius = tokens.some((token: DeviceToken) => {
          if (!token.lat || !token.lng) return true; // If no location, send anyway
          const distance = haversine(token.lat, token.lng, store.lat, store.lng);
          return distance <= entry.radius_miles;
        });

        if (!withinRadius) continue;

        // 6. Send push notification via FCM
        const title = `ð¨ ${product.name} Restocked!`;
        const body = `Spotted at ${store.name} (${store.city}, ${store.state}) â ${event.new_quantity > 1 ? event.new_quantity + " units" : "Limited stock"}`;

        for (const token of tokens as DeviceToken[]) {
          const success = await sendFCMNotification(token.fcm_token, {
            title,
            body,
            data: {
              type: "restock_alert",
              product_id: event.product_id,
              store_id: event.store_id,
              event_id: event.id,
            },
          });

          if (success) {
            totalSent++;

            // Log the notification
            await supabase.from("notification_log").insert({
              user_id: entry.user_id,
              restock_event_id: event.id,
              product_name: product.name,
              store_name: store.name,
              status: "sent",
            });
          }
        }
      }

      // Update event with notification count
      await supabase
        .from("restock_events")
        .update({ notifications_sent: totalSent })
        .eq("id", event.id);
    }

    console.log(`Done! Sent ${totalSent} push notifications`);

    return new Response(
      JSON.stringify({ sent: totalSent, events_processed: events.length }),
      { status: 200, headers: { "Content-Type": "application/json" } }
    );
  } catch (error) {
    console.error("Edge function error:", error);
    return new Response(JSON.stringify({ error: String(error) }), {
      status: 500,
      headers: { "Content-Type": "application/json" },
    });
  }
});

// ââ FCM V1 Push Notification âââââââââââââââââââââââââââââââââââââ

async function sendFCMNotification(
  token: string,
  notification: { title: string; body: string; data?: Record<string, string> }
): Promise<boolean> {
  if (!FCM_SERVICE_ACCOUNT_B64) {
    console.warn("FCM_SERVICE_ACCOUNT not set â skipping push notification");
    return false;
  }

  try {
    const accessToken = await getAccessToken();

    const response = await fetch(
      `https://fcm.googleapis.com/v1/projects/${FCM_PROJECT_ID}/messages:send`,
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${accessToken}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          message: {
            token,
            notification: {
              title: notification.title,
              body: notification.body,
            },
            data: notification.data || {},
            apns: {
              headers: {
                "apns-priority": "10",
              },
              payload: {
                aps: {
                  sound: "default",
                  badge: 1,
                  "content-available": 1,
                },
              },
            },
          },
        }),
      }
    );

    if (!response.ok) {
      const text = await response.text();
      console.error(`FCM V1 error: ${response.status} ${text}`);
      return false;
    }

    return true;
  } catch (error) {
    console.error(`FCM send error: ${error}`);
    return false;
  }
}

// ââ Haversine Distance (miles) âââââââââââââââââââââââââââââââââ

function haversine(
  lat1: number, lng1: number,
  lat2: number, lng2: number
): number {
  const R = 3958.8; // Earth radius in miles
  const dLat = toRad(lat2 - lat1);
  const dLng = toRad(lng2 - lng1);
  const a =
    Math.sin(dLat / 2) * Math.sin(dLat / 2) +
    Math.cos(toRad(lat1)) *
      Math.cos(toRad(lat2)) *
      Math.sin(dLng / 2) *
      Math.sin(dLng / 2);
  const c = 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
  return R * c;
}

function toRad(deg: number): number {
  return (deg * Math.PI) / 180;
}
