"use client";

import Link from "next/link";
import { TrivialityLogo } from "@/components/triviality-logo";
import { TextFlippingBoard } from "@/components/ui/text-flipping-board";

export default function Home() {
  return (
    <main className="relative min-h-screen overflow-hidden bg-white text-[#111]">
      <nav className="relative z-10 flex items-center justify-between px-6 py-6 sm:px-10 lg:px-14">
        <TrivialityLogo />

        <div className="flex items-center text-[10px] font-medium uppercase tracking-[0.2em]">
          <Link
            className="border-b border-black pb-1 transition-opacity hover:opacity-50"
            href="/login"
          >
            Login
          </Link>
        </div>
      </nav>

      <section className="relative z-10 flex min-h-[calc(100vh-88px)] items-center justify-center px-6 pb-20 pt-8 sm:px-10">
        <div className="w-full max-w-5xl">
          <TextFlippingBoard
            text="TRIVIALITY"
            className="!mx-auto !max-w-5xl rounded-none bg-white p-0 shadow-none"
          />
        </div>

      </section>

    </main>
  );
}
