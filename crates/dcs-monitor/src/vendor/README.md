# Pinned browser JSON parser

`lossless-json.js` is the UMD bundle from the MIT-licensed
[`lossless-json` 4.3.1](https://github.com/josdejong/lossless-json/tree/v4.3.1)
npm distribution. Its upstream source revision is
`a19ae09763876582d120d2f3de4cbd7741faa427`. The included
`lossless-json.LICENSE.md` carries the upstream license.

Source archive:
`https://registry.npmjs.org/lossless-json/-/lossless-json-4.3.1.tgz`.
Registry SHA-512 integrity, checked before extraction:
`sha512-SqD/Bg3ZfltBJ2Z14hJ/BihnvtV553WO4g9/ePtlp4lrnl9jF3AdIJt53A/Wkg/0Li+LMfxaBqgx1MiFZdQlpQ==`.

| File | SHA-256 |
| --- | --- |
| Source archive | `f71665240257ee183b98fd1b4e0a7cb81156ddff3f01070ea9331258902c1e5c` |
| Original browser bundle | `3eebf0821060a4d9910c55e6c50f11961062cc20944ee745007ea19e3cdec885` |
| Property-patched bundle before license header | `af38c4262ca01b53bb3fb8640c4d91cb7d56155753b009629ba534d2a18294e7` |
| Patched browser bundle with license served here | `9f2972292943cc6f38c4427832147368e8887c84c88ad642e2143a9f67bb61c5` |
| License | `c5075e9ebeac2efabaddeb7ff8f215ef180f76da7837a79e3107868271693114` |

The generic monitor embeds and serves this bundle from the same origin. No
customer download, CDN, npm install or JavaScript configuration is required.
`wire-json.js` retains exact decimal strings only for receipt submission
identities; ordinary values remain native Numbers. This does not establish
full-u64 UI support for point IDs, ticks or integer process values. Malformed
JSON, conflicting duplicate keys and invalid submission identities fail the consumer read.
They cannot delay a control scan or justify a command retry.

The upstream object parser assigns properties with `object[key] = value`.
For the valid JSON key `__proto__`, that sets the object's prototype and drops
the own field. DCS parameter and event maps permit that key. Apply this one
literal replacement to the original bundle (exactly one occurrence) so both
new and replaced members are enumerable, writable, configurable own data
properties, as with native JSON parsing:

```js
// Original
void 0!==n&&(e[r]=n)}else e[r]=o
// Replacement
void 0!==n&&Object.defineProperty(e,r,{value:n,enumerable:true,writable:true,configurable:true})}else Object.defineProperty(e,r,{value:o,enumerable:true,writable:true,configurable:true})
```

Prepend a `/*! ... */` comment containing the version, patch reference and full
included MIT notice, so the embedded binary also serves the required license.
The property insertion is the only local semantic library patch. Production contract tests pin the served
digest and prove ordinary object prototypes and dangerous-key behavior. The
license is unchanged; this does not claim an upstream fix or security audit.

Update by obtaining the exact versioned npm archive, checking its registry
integrity, applying or retiring the documented patch, updating these hashes,
and running the UI contract and served-asset gates. No runtime dependency or
bundle build pipeline is added.
