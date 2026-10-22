using Ardalis.GuardClauses;

const string expected = "ready";
var accepted = Guard.Against.Null(expected);
if (accepted != expected)
{
    throw new InvalidOperationException("A valid value was not preserved.");
}

ExpectThrows<ArgumentNullException>(() => Guard.Against.Null<string?>(null));
ExpectThrows<ArgumentException>(() => Guard.Against.Negative(-1));
Console.WriteLine("GuardClauses smoke passed.");

static void ExpectThrows<TException>(Action action) where TException : Exception
{
    try
    {
        action();
    }
    catch (TException)
    {
        return;
    }

    throw new InvalidOperationException($"Expected {typeof(TException).Name}.");
}
