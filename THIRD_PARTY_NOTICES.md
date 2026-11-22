# Third-Party Notices

Nollpost depends on open-source libraries declared in `pyproject.toml` and shares
an optional Apple Mail locking convention and AppleScript escaping pattern with
the following project:

## apple-mail-mcp

- **Repository:** https://github.com/patrickfreyer/apple-mail-mcp
- **License:** MIT License
- **Copyright:** Copyright (c) 2025 Patrick Freyer

Nollpost's `mail_automation_lock()` advisory `fcntl.flock` pattern (allowing
safe concurrent use alongside `apple-mail-mcp`) and `escape_applescript()`
string-escaping sequence follow the MIT-licensed implementation in
`apple-mail-mcp`:

> Permission is hereby granted, free of charge, to any person obtaining a copy
> of this software and associated documentation files (the "Software"), to deal
> in the Software without restriction, including without limitation the rights
> to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
> copies of the Software, and to permit persons to whom the Software is
> furnished to do so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in all
> copies or substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
> IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
> FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
> AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
> LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
> OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
> SOFTWARE.
