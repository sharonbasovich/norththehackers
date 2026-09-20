"use client";

import React, { FormEvent } from "react";
import { useRouter } from "next/navigation";
import { TrivialityLogo } from "@/components/triviality-logo";

export default function LoginPage() {
  const router = useRouter();

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    router.push("/dashboard");
  };

  return (
    <main className="relative min-h-screen bg-white text-[#111]">
      <section className="absolute left-6 top-6 z-10 sm:left-12">
        <TrivialityLogo />
      </section>

      <section className="relative flex min-h-screen items-center justify-center px-6 py-16 sm:px-12 lg:px-16 xl:px-24">
        <div className="w-full max-w-md">
          <div className="mb-9">
            <h1 className="text-3xl font-semibold tracking-[-0.05em] sm:text-4xl">
              Sign in to Triviality
            </h1>
          </div>

          <form onSubmit={handleSubmit} className="space-y-5">
            <div className="space-y-2">
              <label htmlFor="email" className="text-sm font-medium">
                Email
              </label>
              <input
                id="email"
                name="email"
                type="email"
                autoComplete="email"
                placeholder="name@example.com"
                required
                className="h-12 w-full rounded-md border border-black/15 bg-white px-4 text-sm outline-none transition-colors placeholder:text-black/35 focus:border-black focus:ring-1 focus:ring-black"
              />
            </div>

            <div className="space-y-2">
              <label htmlFor="password" className="text-sm font-medium">
                Password
              </label>
              <input
                id="password"
                name="password"
                type="password"
                autoComplete="current-password"
                placeholder="Enter your password"
                required
                className="h-12 w-full rounded-md border border-black/15 bg-white px-4 text-sm outline-none transition-colors placeholder:text-black/35 focus:border-black focus:ring-1 focus:ring-black"
              />
            </div>

            <button
              type="submit"
              className="h-12 w-full rounded-md bg-black text-sm font-medium text-white transition-colors hover:bg-black/75"
            >
              Sign in
            </button>

          </form>

          <p className="mt-6 text-center text-sm text-black/50">
            Don&apos;t have an account?{" "}
            <button
              type="button"
              className="font-medium text-black underline underline-offset-4 transition-opacity hover:opacity-60"
            >
              Create account
            </button>
          </p>
        </div>
      </section>
    </main>
  );
}
