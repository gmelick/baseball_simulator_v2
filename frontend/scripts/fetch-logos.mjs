// fetch-logos.mjs — SIM-519 Part T. Run once by hand: `node scripts/fetch-logos.mjs`.
// Downloads each club's logo from MLB's static host into public/logos/{id}.svg.
// The SVGs are committed, so the running page never calls the outside host.
import { mkdir, writeFile } from 'node:fs/promises'

const IDS = [108, 109, 110, 111, 112, 113, 114, 115, 116, 117, 118, 119, 120, 121, 133,
  134, 135, 136, 137, 138, 139, 140, 141, 142, 143, 144, 145, 146, 147, 158]
const OUT = new URL('../public/logos/', import.meta.url)

await mkdir(OUT, { recursive: true })
let failed = 0
for (const id of IDS) {
  const res = await fetch(`https://www.mlbstatic.com/team-logos/${id}.svg`)
  const body = await res.text()
  if (!res.ok || !body.trimStart().startsWith('<')) {
    console.error(`${id}: HTTP ${res.status}`)
    failed += 1
    continue
  }
  await writeFile(new URL(`${id}.svg`, OUT), body)
  console.log(`${id}: ${body.length} bytes`)
}
process.exit(failed ? 1 : 0)
