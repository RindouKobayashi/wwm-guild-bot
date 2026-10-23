using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using SteamKit2;
using SteamKit2.Authentication;

// No desktop Steam IPC, game DLLs, or game process access.
if (args.Length != 2 || args[0] is not ("setup" or "probe" or "ticket"))
{
    Console.Error.WriteLine("Usage: HeadlessSteam setup|probe|ticket /absolute/path/to/context.json");
    return 2;
}
var contextPath = Path.GetFullPath(args[1]);
using var context = JsonDocument.Parse(File.ReadAllText(contextPath));
var root = context.RootElement;
if (root.GetProperty("account_kind").GetString() != "test") return 2;
var expected = root.GetProperty("expected_steamid_sha256").GetString()!;
if (expected.Length != 64) return 2;
var credentialsPath = Path.Combine(Path.GetDirectoryName(contextPath)!, "steam-headless.json");
var setup = args[0] == "setup";
string username, refresh = "";
if (setup)
{
    Console.Error.Write("TEST Steam account name: ");
    username = Console.ReadLine() ?? "";
}
else
{
    if (!OperatingSystem.IsWindows() && (File.GetUnixFileMode(credentialsPath) &
        (UnixFileMode.GroupRead | UnixFileMode.GroupWrite | UnixFileMode.OtherRead | UnixFileMode.OtherWrite)) != 0)
        throw new InvalidOperationException("Private Steam credential permissions must be 600");
    using var saved = JsonDocument.Parse(File.ReadAllText(credentialsPath));
    if (saved.RootElement.GetProperty("expected_steamid_sha256").GetString() != expected) return 2;
    username = saved.RootElement.GetProperty("username").GetString()!;
    refresh = saved.RootElement.GetProperty("refresh_token").GetString()!;
}
var client = new SteamClient();
var callbacks = new CallbackManager(client);
var user = client.GetHandler<SteamUser>()!;
var finished = false;
var result = 1;
var deadline = DateTime.UtcNow.AddMinutes(setup ? 5 : 2);
callbacks.Subscribe<SteamClient.DisconnectedCallback>(_ => finished = true);
callbacks.Subscribe<SteamClient.ConnectedCallback>(async _ =>
{
    try
    {
        if (setup)
        {
            Console.Error.Write("TEST account password (hidden): ");
            var password = new StringBuilder();
            while (true)
            {
                var key = Console.ReadKey(true);
                if (key.Key == ConsoleKey.Enter) break;
                if (key.Key == ConsoleKey.Backspace) { if (password.Length > 0) password.Length--; }
                else if (!char.IsControl(key.KeyChar)) password.Append(key.KeyChar);
            }
            Console.Error.WriteLine();
            var session = await client.Authentication.BeginAuthSessionViaCredentialsAsync(new AuthSessionDetails
            {
                Username = username, Password = password.ToString(), IsPersistentSession = true,
                Authenticator = new UserConsoleAuthenticator()
            });
            password.Clear();
            var auth = await session.PollingWaitForResultAsync();
            username = auth.AccountName;
            refresh = auth.RefreshToken;
        }
        user.LogOn(new SteamUser.LogOnDetails
        { Username = username, AccessToken = refresh, ShouldRememberPassword = true });
    }
    catch { Console.Error.WriteLine("Headless Steam authentication failed; no credentials saved."); finished = true; }
});
callbacks.Subscribe<SteamUser.LoggedOnCallback>(async loggedOn =>
{
    try
    {
        if (loggedOn.Result != EResult.OK) throw new InvalidOperationException();
        var identity = client.SteamID!.ConvertToUInt64().ToString(System.Globalization.CultureInfo.InvariantCulture);
        var actual = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(identity))).ToLowerInvariant();
        if (!CryptographicOperations.FixedTimeEquals(Encoding.ASCII.GetBytes(actual), Encoding.ASCII.GetBytes(expected)))
        { Console.Error.WriteLine("Wrong account: refused ticket and credential save."); return; }
        if (setup)
        {
            var temporary = credentialsPath + "." + Guid.NewGuid().ToString("N") + ".tmp";
            try
            {
                var options = new FileStreamOptions { Mode = FileMode.CreateNew, Access = FileAccess.Write };
                if (!OperatingSystem.IsWindows()) options.UnixCreateMode = UnixFileMode.UserRead | UnixFileMode.UserWrite;
                using (var stream = new FileStream(temporary, options))
                    JsonSerializer.Serialize(stream, new { username, refresh_token = refresh, expected_steamid_sha256 = expected });
                File.Move(temporary, credentialsPath, true);
            }
            finally { if (File.Exists(temporary)) File.Delete(temporary); }
            Console.WriteLine("Test account verified; private renewable Steam login saved.");
            result = 0;
        }
        else
        {
            using var ticket = await client.GetHandler<SteamAuthTicket>()!.GetAuthTicketForWebApi(3564740, "");
            if (args[0] == "ticket")
            {
                if (!Console.IsOutputRedirected || !Console.IsInputRedirected)
                    throw new InvalidOperationException("Ticket mode requires private process pipes");
                Console.WriteLine(JsonSerializer.Serialize(new { ticket = Convert.ToBase64String(ticket.Ticket),
                    expected_steamid_sha256 = expected }));
                Console.Out.Flush();
                // Parent releases only after the SDK exchange; EOF also cancels.
                await Console.In.ReadLineAsync().WaitAsync(TimeSpan.FromSeconds(30));
            }
            else
                Console.WriteLine(JsonSerializer.Serialize(new { status = "headless_ticket_received", ticket_bytes = ticket.Ticket.Length,
                    account_guard_verified = true, ticket_values_saved = false, sdk_login_requests = false }));
            result = 0;
        }
    }
    catch { Console.Error.WriteLine("Headless account/ticket check failed; no WWM request sent."); }
    finally { user.LogOff(); finished = true; }
});
try
{
    client.Connect();
    while (!finished && DateTime.UtcNow < deadline) callbacks.RunWaitCallbacks(TimeSpan.FromMilliseconds(100));
}
finally { client.Disconnect(); }
return result;
