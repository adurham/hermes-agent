'use strict'

// Shim for the deprecated boolean@3.2.0 package (only real consumers in this
// tree: electron's @electron/get -> global-agent, and roarr, both via
// `require('boolean').boolean`). Reimplements the exact same truthy-string
// parsing logic directly -- no third-party dependency.
function boolean(value) {
  switch (Object.prototype.toString.call(value)) {
    case '[object String]':
      return ['true', 't', 'yes', 'y', 'on', '1'].includes(value.trim().toLowerCase())
    case '[object Number]':
      return value.valueOf() === 1
    case '[object Boolean]':
      return value.valueOf()
    default:
      return false
  }
}

function isBooleanable(value) {
  switch (Object.prototype.toString.call(value)) {
    case '[object String]':
      return ['true', 't', 'yes', 'y', 'on', '1', 'false', 'f', 'no', 'n', 'off', '0'].includes(
        value.trim().toLowerCase()
      )
    case '[object Number]':
      return [0, 1].includes(value.valueOf())
    case '[object Boolean]':
      return true
    default:
      return false
  }
}

module.exports = { boolean, isBooleanable }
