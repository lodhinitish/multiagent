import { api, html, mount, setCurrentUser } from "../lib.js";

// #/login?next=agents — username/password sign-in
export async function loginView(root, _params, query) {
  document.body.classList.add("auth-page");
  mount(root, html`
    <div class="login-wrap">
      <form class="panel login" id="login" novalidate>
        <div class="brand"><span class="brand-mark" aria-hidden="true"></span><span>Agent Studio</span></div>
        <h1>Log in</h1>
        <div class="banner error" id="err" role="alert" hidden></div>
        <label class="field">Username
          <input name="username" autocomplete="username" autocapitalize="none" spellcheck="false" required autofocus></label>
        <label class="field">Password
          <input name="password" type="password" autocomplete="current-password" required></label>
        <button class="btn primary" type="submit" id="submit">Log in</button>
      </form>
    </div>`);

  const form = root.querySelector("#login"), err = root.querySelector("#err"), submit = root.querySelector("#submit");
  form.username.focus();
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const username = form.username.value.trim(), password = form.password.value;
    if (!username || !password) { err.textContent = "Enter your username and password."; err.hidden = false; return; }
    submit.disabled = true;
    submit.textContent = "Logging in…";
    try {
      const res = await api("/auth/login", { method: "POST", body: { username, password } });
      setCurrentUser(res.user);
      const next = query.get("next");
      location.hash = `#/${next && !next.startsWith("login") ? next : "agents"}`;
    } catch (ex) {
      err.textContent = ex.message;
      err.hidden = false;
      form.password.value = "";
      form.password.focus();
      submit.disabled = false;
      submit.textContent = "Log in";
    }
  });

  return () => document.body.classList.remove("auth-page");
}
