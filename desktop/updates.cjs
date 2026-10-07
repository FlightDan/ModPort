'use strict';
const https = require('node:https');

const RELEASE_ENDPOINT = 'https://api.github.com/repos/FlightDan/ModPort/releases/latest';
const MAX_RESPONSE_BYTES = 1024 * 1024;
const CHECK_TIMEOUT_MS = 10000;

// Stable release tags use SemVer. Build metadata has no effect on precedence.
function parseStableVersion(value) {
  if (typeof value !== 'string' || value.length > 128) return null;
  const match = /^v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$/.exec(value);
  return match && match[0] === value ? {version: match.slice(1, 4).join('.'), parts: match.slice(1, 4).map(BigInt)} : null;
}

function compareStableVersions(left, right) {
  const first = parseStableVersion(left), second = parseStableVersion(right);
  if (!first || !second) throw new Error('invalid_version');
  for (let index = 0; index < 3; index++) {
    if (first.parts[index] !== second.parts[index]) return first.parts[index] > second.parts[index] ? 1 : -1;
  }
  return 0;
}

function validatedReleaseUrl(value, tag) {
  if (typeof value !== 'string' || !parseStableVersion(tag)) return null;
  // Require the exact repository/tag path. No credentials, redirects, encoded
  // paths, query parameters or alternate origins reach shell.openExternal.
  const expected = `https://github.com/FlightDan/ModPort/releases/tag/${tag}`;
  return value === expected ? expected : null;
}

function updateError(code) {
  return Object.assign(new Error(code), {updateCode: code});
}

class UpdateChecker {
  constructor({currentVersion, request = https.request, timeoutMs = CHECK_TIMEOUT_MS,
    now = () => new Date().toISOString()} = {}) {
    this.request = request;
    this.timeoutMs = timeoutMs;
    this.now = now;
    this.listeners = new Set();
    this.pending = null;
    this.cancelRequest = null;
    this.disposed = false;
    this.state = {status: 'idle', currentVersion, latestVersion: null,
      releaseUrl: null, checkedAt: null, errorCode: null};
  }

  getStatus() {return {...this.state};}

  subscribe(listener) {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  publish(next) {
    this.state = {...this.state, ...next};
    for (const listener of this.listeners) {
      try {listener(this.getStatus());} catch (_) { /* Observers cannot fail a check. */ }
    }
  }

  fetchRelease() {
    return new Promise((resolve, reject) => {
      let request, response, timer, finished = false;
      const finish = (error, release) => {
        if (finished) return;
        finished = true;
        clearTimeout(timer);
        this.cancelRequest = null;
        if (error) {
          reject(error);
          if (response) response.destroy();
          if (request) request.destroy();
        } else resolve(release);
      };
      this.cancelRequest = () => finish(updateError('cancelled'));
      timer = setTimeout(() => finish(updateError('timeout')), this.timeoutMs);
      timer.unref?.();
      try {
        // Native HTTPS has no browser cookie jar or inherited authorization.
        // Redirects are intentionally not followed.
        request = this.request(RELEASE_ENDPOINT, {method: 'GET', headers: {
          Accept: 'application/vnd.github+json',
          'X-GitHub-Api-Version': '2022-11-28',
          'User-Agent': 'ModPort-Desktop-Update-Check',
        }}, incoming => {
          response = incoming;
          if (incoming.statusCode !== 200) {
            const code = incoming.statusCode === 404 ? 'no_releases'
              : [403, 429].includes(incoming.statusCode) ? 'rate_limited' : 'http_error';
            finish(updateError(code));
            return;
          }
          let size = 0;
          const chunks = [];
          incoming.on('data', chunk => {
            if (finished) return;
            size += chunk.length;
            if (size > MAX_RESPONSE_BYTES) {finish(updateError('response_too_large')); return;}
            chunks.push(chunk);
          });
          incoming.on('end', () => {
            if (finished) return;
            try {finish(null, JSON.parse(Buffer.concat(chunks).toString('utf8')));}
            catch (_) {finish(updateError('invalid_release'));}
          });
          incoming.on('error', () => finish(updateError('offline')));
          incoming.on('aborted', () => finish(updateError('offline')));
          incoming.on('close', () => {if (!finished) finish(updateError('offline'));});
        });
        request.on('error', () => finish(updateError('offline')));
        request.end();
      } catch (_) {finish(updateError('offline'));}
    });
  }

  check() {
    if (this.disposed) return Promise.resolve(this.getStatus());
    if (this.pending) return this.pending;
    this.pending = Promise.resolve().then(async () => {
      if (this.disposed) return this.getStatus();
      try {
        if (!parseStableVersion(this.state.currentVersion)) throw updateError('invalid_version');
        const release = await this.fetchRelease();
        if (this.disposed) return this.getStatus();
        const latest = parseStableVersion(release?.tag_name);
        const releaseUrl = validatedReleaseUrl(release?.html_url, release?.tag_name);
        if (!latest || !releaseUrl || release.draft !== false || release.prerelease !== false) {
          throw updateError('invalid_release');
        }
        this.publish({status: compareStableVersions(latest.version, this.state.currentVersion) > 0 ? 'available' : 'current',
          latestVersion: latest.version, releaseUrl, checkedAt: this.now(), errorCode: null});
      } catch (error) {
        if (!this.disposed) this.publish({status: 'error', latestVersion: null,
          releaseUrl: null, checkedAt: this.now(), errorCode: error.updateCode || 'offline'});
      }
      return this.getStatus();
    }).finally(() => {this.pending = null;});
    this.publish({status: 'checking', errorCode: null});
    return this.pending;
  }

  getDownloadUrl() {
    if (this.state.status !== 'available' || !this.state.releaseUrl) return null;
    return this.state.releaseUrl;
  }

  dispose() {
    this.disposed = true;
    this.listeners.clear();
    this.cancelRequest?.();
  }
}

module.exports = {UpdateChecker, compareStableVersions, parseStableVersion,
  validatedReleaseUrl, RELEASE_ENDPOINT, MAX_RESPONSE_BYTES};
