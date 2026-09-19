/**
 * One location reading, for confirming an arrival. Never a subscription.
 *
 * `getCurrentPosition`, not `watchPosition` — the difference is the whole
 * privacy posture of this feature. A watch is a trail; a single call is a
 * question asked once and answered once, and the answer is turned into a
 * distance on the server and discarded.
 *
 * **It never rejects.** Every way this can fail — the permission refused, no
 * hardware, a timeout in a basement, an insecure origin, a browser that has no
 * geolocation at all — resolves to `null`, because all of them mean the same
 * thing to the product: no conclusion is available. A caller that had to catch
 * them would eventually treat one of them as a failure to arrive, which is the
 * thing the three-valued check exists to prevent.
 */
//: **Short on purpose, and the reason is the person holding the phone.**
//: This blocks the "I'm on site" tap, and a cleaner standing at a door with a
//: button that appears not to have worked will tap it again. Three seconds is
//: long enough for a phone that already has permission — the common case, from
//: the second job onwards — and a fix that has not arrived by then is one we
//: are happy to do without, because `unchecked` is an honest answer and
//: waiting is not.
//:
//: A browser that never answers at all is real: headless Chromium with no
//: location provider does exactly that, which is how this was found.
const TIMEOUT_MS = 3000

export function whereAmI() {
  return new Promise((resolve) => {
    if (typeof navigator === 'undefined' || !navigator.geolocation) {
      resolve(null)
      return
    }

    let settled = false
    const answer = (value) => {
      if (settled) return
      settled = true
      resolve(value)
    }

    // A belt-and-braces deadline. `timeout` below covers the usual case, but
    // a browser that never calls either callback would otherwise leave the tap
    // spinning — and marking yourself on site must not wait on a permission
    // prompt somebody has walked away from. Headless Chromium with no location
    // provider does precisely this.
    const timer = setTimeout(() => answer(null), TIMEOUT_MS + 500)

    navigator.geolocation.getCurrentPosition(
      (position) => {
        clearTimeout(timer)
        answer({
          lat: position.coords.latitude,
          lng: position.coords.longitude,
          // Sent as the browser reports it. The server refuses to draw a
          // conclusion from a fix too coarse to support one, which it can only
          // do if this is honest rather than optimistic.
          accuracy_m: position.coords.accuracy,
        })
      },
      () => {
        clearTimeout(timer)
        answer(null)
      },
      { enableHighAccuracy: true, timeout: TIMEOUT_MS, maximumAge: 0 },
    )
  })
}
