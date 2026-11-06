import { api, currentUser, enc, html, mount, setCurrentUser, toast } from "../lib.js";

// #/account — change your password; admins also manage users here
export async function accountView(root) {
  const me = await currentUser();
  const users = me.role === "admin" ? await api("/auth/users") : null;

  mount(root, html`
    <div class="page-head"><div><h1>Account</h1>
      <p>Logged in as <strong>${me.username}</strong> (${me.role}).</p></div></div>

    <section class="panel stack" style="max-width:560px">
      <h2>Change password</h2>
      <form id="pw" class="stack">
        <label class="field">Current password <input name="current" type="password" autocomplete="current-password" required></label>
        <label class="field">New password <input name="next" type="password" autocomplete="new-password" minlength="8" required>
          <span class="hint">At least 8 characters. Other devices will be logged out.</span></label>
        <div><button class="btn primary" type="submit">Change password</button></div>
      </form>
    </section>

    ${users ? html`
    <section class="panel stack" style="margin-top:20px">
      <h2>Users</h2>
      <table><thead><tr><th>Username</th><th>Role</th><th>Created</th><th></th></tr></thead><tbody>
        ${users.map((u) => html`<tr>
          <td class="mono">${u.username}</td><td>${u.role}</td>
          <td>${new Date(u.created_at).toLocaleDateString()}</td>
          <td style="text-align:right">${u.username === me.username ? html`<span class="hint">you</span>` : html`
            <button class="btn small" data-reset="${u.username}">Reset password</button>
            <button class="btn small danger" data-delete="${u.username}">Delete</button>`}</td>
        </tr>`)}
      </tbody></table>
      <h3 style="margin-top:8px">Add user</h3>
      <form id="add" class="grid-3" style="align-items:end">
        <label class="field">Username <input name="username" autocomplete="off" autocapitalize="none" spellcheck="false" required></label>
        <label class="field">Password <input name="password" type="password" autocomplete="new-password" minlength="8" required></label>
        <label class="field">Role <select name="role"><option value="user">user</option><option value="admin">admin</option></select></label>
        <div><button class="btn primary" type="submit">Add user</button></div>
      </form>
    </section>` : ""}`);

  root.querySelector("#pw").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    try {
      const res = await api("/auth/password", { method: "POST", body: { current_password: f.current.value, new_password: f.next.value } });
      setCurrentUser(res.user);
      f.reset();
      toast("Password changed.", "ok");
    } catch (ex) { toast(ex.message, "error"); }
  });

  if (!users) return;
  const reload = () => accountView(root);
  root.querySelector("#add").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    try {
      await api("/auth/users", { method: "POST", body: { username: f.username.value.trim(), password: f.password.value, role: f.role.value } });
      toast(`User '${f.username.value.trim()}' added.`, "ok");
      reload();
    } catch (ex) { toast(ex.message, "error"); }
  });
  root.querySelectorAll("[data-delete]").forEach((b) => b.addEventListener("click", async () => {
    const name = b.dataset.delete;
    if (!confirm(`Delete user '${name}'? They will be logged out.`)) return;
    try { await api(`/auth/users/${enc(name)}`, { method: "DELETE" }); toast(`Deleted '${name}'.`, "ok"); reload(); }
    catch (ex) { toast(ex.message, "error"); }
  }));
  root.querySelectorAll("[data-reset]").forEach((b) => b.addEventListener("click", async () => {
    const name = b.dataset.reset;
    const password = prompt(`New password for '${name}' (at least 8 characters):`);
    if (!password) return;
    try { await api(`/auth/users/${enc(name)}/password`, { method: "PUT", body: { password } }); toast(`Password reset for '${name}'.`, "ok"); }
    catch (ex) { toast(ex.message, "error"); }
  }));
}
