/**
 * CollectLocal — Restock Push Notification Edge Function
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
const FCM_SERVER_KEY = Deno.env.get("FCM_SERVER_KEY") || "";

const supabase = createClient(SUPABASE_URL, SUPABASE_SERVICE_KEY);

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
      const { data: product } = await supabase
        .from("restock_products")
        .select("*")
        .eq("id", event.product_id)
        .single();

      const { data: store } = await supabase
        .from("retail_stores")
        .select("*")
        .eq("id", event.store_id)
        .single();

      if (!product || !store) {
        console.error(`Product or store not found for event ${event.id}`);
        continue;
      }

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

      for (const entry of waitlist as WaitlistEntry[]) {
        const { data: tokens } = await supabase
          .from("device_tokens")
          .select("*")
          .eq("user_id", entry.user_id);

        if (!tokens?.length) continue;

        const withinRadius = tokens.some((token: DeviceToken) => {
          if (!token.lat || !token.lng) return true;
          const distance = haversine(token.lat, token.lng, store.lat, store.lng);
          return distance <= entry.radius_miles;
        });

        if (!withinRadius) continue;

        const title = `${product.name} Restocked!`;
        const body = `Spotted at ${store.name} (${store.city}, ${store.state}) — ${event.new_quantity > 1 ? event.new_quantity + " units" : "Limited stock"}`;

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

// ── FCM Push Notification ──────────────────────────────────────

async function sendFCMNotification(
  token: string,
  notification: { title: string; body: string; data?: Record<string, string> }
): Promise<boolean> {
  if (!FCM_SERVER_KEY) {
    console.warn("FCM_SERVER_KEY not set — skipping push notification");
    return false;
  }

  try {
    const response = await fetch("https://fcm.googleapis.com/fcm/send", {
      method: "POST",
      headers: {
        Authorization: `key=${FCM_SERVER_KEY}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        to: token,
        notification: {
          title: notification.title,
          body: notification.body,
          sound: "default",
          badge: 1,
        },
        data: notification.data || {},
        content_available: true,
        priority: "high",
      }),
    });

    if (!response.ok) {
      const text = await response.text();
      console.error(`FCM error: ${response.status} ${text}`);
      return false;
    }

    const result = await response.json();
    return result.success === 1;
  } catch (error) {
    console.error(`FCM send error: ${error}`);
    return false;
  }
}

// ── Haversine Distance (miles) ─────────────────────────────────

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
