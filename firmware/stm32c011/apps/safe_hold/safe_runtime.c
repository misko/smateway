/*
 * The reviewed ST startup calls this hook before main. Safe-hold has no C or
 * C++ constructors, so an empty implementation avoids linking a hosted C
 * runtime and keeps the first image deliberately small and auditable.
 */
void __libc_init_array(void)
{
}
